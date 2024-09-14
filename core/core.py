import importlib
import json
import threading
import time
import ollama
import queue
from datetime import datetime
import os

# wikipedia search
import wikipedia

# google search
from googlesearch import search

# for the web request/search sections:
import requests
from bs4 import BeautifulSoup
from requests_html import HTMLSession
import urllib.parse

# home assistant API link
from homeassistant_api import Client as HAClient

# our precontext and tools info:
from config import precontext, tools


class CoreProcessor:
    def __init__(self):
        self.sessions = {}

        #self.input_queue = queue.Queue()  # Input queue for receiving data
        #self.response_queue = queue.Queue()  # Response queue for sending results
        #self.response_finished = threading.Event()  # To signal when the response is complete

        self.model = "llama3.1"
        self.ollama_client = ollama.Client(host='http://192.168.20.200:11434')
        self.pre_context = precontext.llama3_context
        self.voice_pre_context = precontext.voice_context
        self.current_conversation = None
        # self.tools = tools.general_tools

        # Home assistant integration
        self.ha_key = self.get_ha_key()
        self.ha_url = 'http://192.168.20.3:8123/api'
        self.home_assistant = HAClient(self.ha_url, self.ha_key)

        self.available_functions = {
            'close_voice_channel': self.close_voice_channel,
            'get_current_time': self.get_current_time,
            'perform_search': self.perform_search,
            'open_website': self.open_website,
            'home_automation_action': self.home_automation_action,
        }

    def create_session(self, session_id):
        print(f'Creating new session with ID: {session_id}')
        # create a new session for each connection inbound to keep histories etc separate:
        self.sessions[session_id] = {
            'conversation_history': [],
            'response_queue': queue.Queue(),
            'response_finished': threading.Event(),
            'close_voice_channel': threading.Event(),  # for flagging channel close from functions in voice mode
        }
    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_ha_key(self):
        """Pulls the Home Assistant API key from file"""
        # Get the directory of the current script (core.py)
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Construct the full path to the home_assistant_api file
        file_path = os.path.join(script_dir, '../config/home_assistant_api')

        with open(file_path, "r") as file:
            for line in file:
                if line.startswith("HA_API_KEY"):
                    return line.split('=')[1].strip().strip('"')

    def add_ha_to_pre_context(self, pre_context):
        """Adds the available Home Assistant connections to the pre-context"""
        new_context_info = self.ha_get_available_switches_and_scenes()
        pre_context += new_context_info
        return pre_context

    def add_voice_to_pre_context(self, pre_context):
        """Adds the voice commands to pre-context"""
        pre_context += self.voice_pre_context()
        return pre_context

    def process_input(self, input_text, session_id, is_voice=False):
        #print("STARTING NEW INPUT")

        # Retrieve the session-specific data - NOT SURE IF NEEDED NOW:
        session = self.get_session(session_id)
        if session is None:
            self.create_session(session_id)
            session = self.get_session(session_id)

        # clear the response_finished event at the start:
        session['response_finished'].clear()

        # as well as close_voice_channel event flag if relevant
        if is_voice:
            session['close_voice_channel'].clear()

        conversation_history = session['conversation_history']

        # if we're starting a new conversation, create the pre-context and instructions:
        if conversation_history is None:
            conversation_history = []

        prompt = self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
            voice=is_voice,
        )

        conversation_history.append({
            'role': 'user',
            'content': input_text,
        })

        # Debugging: Print the formatted conversation context:
        # print(f"Generated prompt: {prompt}")

        # now we construct the tools:
        if is_voice:
            # it seems order is important, putting voice close channel tool first:
            prompt_tools = tools.voice_tools + tools.general_tools
        else:
            prompt_tools = tools.general_tools

        # debugging:
        # print(f"PROMPT TOOLS=\n\n {prompt_tools}")

        while True:
            full_response, tool_calls = self.send_to_ollama(prompt_text=prompt, prompt_tools=prompt_tools, session=session)

            if full_response:
                conversation_history.append({
                    'role': 'assistant',
                    'content': full_response
                })

            if tool_calls:
                # Debugging:
                # print(f"TOOL CALLS RECEIVED: {tool_calls}")

                for tool in tool_calls:
                    try:
                        function_to_call = self.available_functions[tool['name']]
                        function_response = function_to_call(tool_args=tool, session=session)

                        if conversation_history:
                            conversation_history.append(
                                {
                                    'role': 'tool',
                                    'content': function_response
                                }
                            )

                    except Exception as e:
                        conversation_history.append(
                            {
                                'role': 'tool',
                                'content': f'Error with tool, or bad use of tool: {e}',
                            }
                        )

                # update the prompt for next spin around for tool call response routines:
                prompt = self.update_prompt(conversation_history, is_voice)
            else:
                break

        # if we break out of loop, set that we've finished to other threads:
        self.send_whole_response("", session)
        session['response_finished'].set()

        # print('\ncore: Finishing processing input and response')

    def create_prompt(self, input_text, conversation_history, voice=False):  # functions_json=None):
        # right now we reload this each time so we can tweak it live, may be unnecessary in future:
        #loadprecontext = importlib.import_module('config.precontext')
        #importlib.reload(loadprecontext)

        full_pre_context = self.pre_context

        if voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info from Home Assistant:
        full_pre_context += self.add_ha_to_pre_context(full_pre_context)

        system_section = {
            'role': 'system',
            'content': full_pre_context,
        }

        history_section = conversation_history

        user_input_section = {
            'role': 'user',
            'content': input_text
        }

        prompt = [system_section] + history_section + [user_input_section]
        return prompt

    def update_prompt(self, conversation_history, voice=False):
        # right now we reload this each time so we can tweak it live, may be unnecessary in future:
        #loadprecontext = importlib.import_module('config.precontext')
        #importlib.reload(loadprecontext)

        full_pre_context = self.pre_context

        if voice:
            full_pre_context += self.voice_pre_context

        # we add this each time so we have up to date info from Home Assistant:
        full_pre_context += self.add_ha_to_pre_context(full_pre_context)

        system_section = {
            'role': 'system',
            'content': full_pre_context,
        }

        history_section = conversation_history

        prompt = [system_section] + history_section
        return prompt

    def send_to_ollama(self, prompt_text, prompt_tools, session):
        """Sends request to Ollama, processes return along with tool calls, streams response to message queue"""
        response_queue = session['response_queue']

        try:
            response_stream = self.ollama_client.chat(
                model=self.model,
                messages=prompt_text,
                stream=True,
                keep_alive="2h",
                tools=prompt_tools
            )

            full_response = ""
            tool_calls = []

            json_accumulator = ""
            json_collecting = False
            json_brackets = 0
            inside_code_block = False  # for when we receive code
            backtick_buffer = ""

            for chunk in response_stream:
                response_content = chunk.get('message', {}).get('content', '')
                full_response += response_content

                if response_content:
                    for char in response_content:
                        # receiving code/not receiving code:
                        if char == '`':
                            backtick_buffer += '`'
                            if backtick_buffer == "```":
                                inside_code_block = not inside_code_block
                                backtick_buffer = "" # reset buffer
                        else:
                            backtick_buffer = ""  # this way we only accumulate on consecutive backticks

                        # receiving json:
                        if char == '{' and not inside_code_block:
                            if not json_collecting:
                                json_collecting = True
                                json_accumulator = ""
                            json_brackets += 1
                        if json_collecting:
                            json_accumulator += char
                        if char == '}' and not inside_code_block:
                            json_brackets -= 1
                            if json_brackets == 0:
                                try:
                                    response_json = json.loads(json_accumulator.strip())
                                    tool_calls.append(response_json)

                                except json.JSONDecodeError:
                                    pass

                    if not json_collecting:
                        # send the non-tool call response chunk back to the response thread live:
                        # print(f'{response_content}', end='')
                        response_queue.put(response_content)


            # return the full response when finished for chat history, along with tool calls to process:
            return full_response, tool_calls

        except Exception as e:
            response_queue.put(f"\nError in processing Ollama response: {e}")
            return f"Error in processing Ollama response: {e}", None

    def send_whole_response(self, response_text, session):
        session['response_queue'].put(f"{response_text}  \n")

    def close_voice_channel(self, tool_args, session):
        print("TOOL: CLOSE COMMS CHANNEL")
        self.send_whole_response("Agent closed channel", session)
        session['close_voice_channel'].set()

    def get_current_time(self, tool_args, session):
        self.send_whole_response("Checking Time", session)
        now = datetime.now()
        now_time = now.strftime('%I:%M%p')
        return json.dumps({'current_time': now_time})

    def perform_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        source = tool_args.get('parameters').get('source')
        num_responses = int(tool_args.get('parameters').get('number', 10))

        if source == 'web':
            self.send_whole_response(f"Performing Web Search: '{query}'", session)
            return self._perform_web_search(query, num_responses)

        elif source == 'wikipedia':
            self.send_whole_response(f"Performing research on Wikipedia on subject: {query}", session)
            return self._perform_wikipedia_search(query)

        else:
            return json.dumps({'error': 'Invalid source specified. Choose "web" or "wikipedia".'})

    def _perform_web_search(self, query, num_responses):
        try:
            # Using the Google search function to get results
            results = []
            for url in search(query, num=num_responses, stop=num_responses, pause=2, country='au'):
                results.append({'link': url})

            if not results:
                results.append({'error': 'no results found, probably web search tool failure'})
            else:
                # Add instruction for the LLM at the beginning of results
                results.insert(0, {
                    'instruction': 'If more information is required, open the websites of interest from the following results.'})

        except Exception as e:
            return json.dumps({'web_search_error': f'Error in web search: {e}'})

        return json.dumps({'web_search_results': results})

    def _perform_wikipedia_search(self, query):
        search_results = wikipedia.search(query)
        results = []

        if search_results:
            for title in search_results:
                try:
                    summary = wikipedia.summary(title, sentences=2)
                    page = wikipedia.page(title)

                    result = {
                        "title": title,
                        "summary": summary,
                        "url": page.url
                    }
                    results.append(result)
                except wikipedia.DisambiguationError:
                    results.append({
                        "title": title,
                        "summary": "Disambiguation page, multiple meanings exist",
                        "url": None
                    })
                except wikipedia.PageError:
                    results.append({
                        "title": title,
                        "summary": "Page does not exist.",
                        "url": None
                    })
        else:
            results.append({'error': 'No results, try another search term'})

        return json.dumps({'wikipedia_search_results': results})


    '''def web_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        num_responses = int(tool_args.get('parameters').get('number', 10))
        self.send_whole_response(f"Performing Web Search: '{query}'", session)

        try:
            # Using the Google search function to get results
            results = []
            for url in search(query, num=num_responses, stop=10, pause=2, country='au'):
                results.append({'link': url})

            if not results:
                results.append({'error': 'no results found, probably web search tool failure'})
            else:
                # Add instruction for the LLM at the beginning of results
                results.insert(0, {
                    'instruction': 'If more information is required, open the websites of interest from the following results.'})

        except Exception as e:
            # Debugging:
            # print(f"ERROR WEB SEARCH CALL: {e}")
            return json.dumps({'web_search_error': f'Error in web search: {e}'})

        return json.dumps({'web_search_results': results})'''

    def open_website(self, tool_args, session, max_retries=3):
        web_session = HTMLSession()
        url = tool_args.get('parameters').get('url')

        for attempt in range(max_retries):
            try:
                response = web_session.get(url)
                response.html.render()
                soup = BeautifulSoup(response.html.html, 'html.parser')
                return json.dumps({'web_link_results': soup.get_text()})
            except requests.exceptions.RequestException as e:
                time.sleep(2)
            except Exception as e:
                return json.dumps({'web_link_error': f'Unexpected error for {url}: {e}'})
        self.send_whole_response(f"Opened Website: {url}", session)
        return json.dumps({'web_link_error': f'Failed to open web link after {max_retries} attempts'})

    '''def wikipedia_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        self.send_whole_response(f"Performing research on Wikipedia on subject: {query}", session)

        search_results = wikipedia.search(query)

        results = []

        if search_results:
            for title in search_results:
                try:
                    summary = wikipedia.summary(title, sentences=2)
                    page = wikipedia.page(title)

                    result = {
                        "title": title,
                        "summary": summary,
                        "url": page.url
                    }
                    results.append(result)
                except wikipedia.DisambiguationError as e:
                    results.append({
                        "title": title,
                        "summary": "Disambiguation page, multiple meanings exist",
                        "url": None
                    })
                except wikipedia.PageError:
                    results.append({
                        "title": title,
                        "summary": "Page does not exist.",
                        "url": None
                    })
        else:
            results.append('No results, try another search term')

        return json.dumps({'wikipedia_search_results': f'{results}'})'''

    def home_automation_action(self, tool_args, session):
        action_type = tool_args.get('parameters').get('action_type')
        entity_id = tool_args.get('parameters').get('entity_id')
        self.send_whole_response(f"Performing '{action_type}' action in Home Assistant", session)

        try:
            if action_type == "set_switch":
                state = tool_args.get('parameters').get('state')
                switch = self.home_assistant.get_domain("switch")
                self.send_whole_response(f"Set switch {switch} to {state}")

                if state == "on":
                    switch.turn_on(entity_id=entity_id)
                else:
                    switch.turn_off(entity_id=entity_id)

                return json.dumps({'home_automation_action': f'Successfully switched {entity_id} {state}'})

            elif action_type == "activate_scene":
                self.send_whole_response(f"Activated Scene '{entity_id}'")
                scene_id = f"scene.{entity_id}"
                scene = self.home_assistant.get_domain("scene")
                scene.turn_on(entity_id=scene_id)

                return json.dumps({'home_automation_action': f'Successfully activated scene {scene_id}'})

            else:
                return json.dumps({'home_automation_action_error': 'Invalid action type specified. Choose "set_switch" or "activate_scene".'})

        except Exception as e:
            self.send_whole_response(f"Error in tool! {e}")
            return json.dumps(
                {'home_automation_action_error': f'Error performing {action_type} on {entity_id}: {str(e)}'})

    '''def ha_set_switch(self, tool_args, session):
        self.send_whole_response("Setting switch in Home Assistant", session)
        entity_id = tool_args.get('parameters').get('entity_id')
        state = tool_args.get('parameters').get('state')

        switch = self.home_assistant.get_domain("switch")

        try:
            if state == "on":
                switch.turn_on(entity_id=entity_id)
            else:
                switch.turn_off(entity_id=entity_id)

            return json.dumps({'set switch': f'successfully switched {entity_id} {state}'})

        except Exception as e:
            return json.dumps({'set switch error': f'Error in switching {entity_id} {state}'})

    def ha_activate_scene(self, tool_args, session):
        self.send_whole_response("Activating Scene in Home Assistant", session)
        scene_id = tool_args.get('parameters').get('scene_id')
        scene = self.home_assistant.get_domain("scene")
        scene_id = f"scene.{scene_id}"

        try:
            scene.turn_on(entity_id=scene_id)
            return json.dumps({'activate scene': f'Successfully activated {scene_id}'})
        except Exception as e:
            return json.dumps({'activate scene error': f'Failed to activate {scene_id}: {str(e)}'})
'''
    def ha_get_available_switches_and_scenes(self):
        """For adding to the end of your pre-context"""
        # Retrieve all states from Home Assistant
        all_states = self.home_assistant.get_states()

        # Filter for switches and scenes
        available_switches = [entity.entity_id for entity in all_states if entity.entity_id.startswith("switch.")]
        available_scenes = [entity.entity_id.split('.')[1] for entity in all_states if
                            entity.entity_id.startswith("scene.")]

        # Format the available switches and scenes into a string
        pre_context_info = (
                "Available Home Automation Entities for use with tools:\n"
                "Available Switch entity_id:\n" +
                "\n".join([f" - {switch}" for switch in available_switches]) +
                "\n\nAvailable Scene entity_id:\n" +
                "\n".join([f" - {scene}" for scene in available_scenes])
        )

        return pre_context_info

    def ha_list_entities_with_states(self):
        # Retrieve all states from Home Assistant
        all_states = self.home_assistant.get_states()

        # Print each entity's ID and current state
        #print("All Entities and Their States:")
        #for entity in all_states:
        #    print(f" - {entity.entity_id}: {entity.state}")

        # Domains of interest (so we don't give the LLM everything
        domains_of_interest = {
            "scene": "Available Lighting Scenes:",
            "switch": "Available Switches with current states:",
            #"light": "Available Lights with current states:",
            "media_player": "Available Media Players with current states:"
        }

        # Organize entities by domain
        categorized_entities = {domain: [] for domain in domains_of_interest}

        for entity in all_states:
            domain = entity.entity_id.split('.')[0]
            if domain in categorized_entities:
                categorized_entities[domain].append(entity)

            # Prepare the output dictionary
            response = {"Available Home Automation objects": {}}

            for domain, header in domains_of_interest.items():
                if categorized_entities[domain]:
                    if domain == "scene":
                        # Include only scene names
                        response["Available Home Automation objects"][header] = [
                            entity.entity_id.split('.')[1] for entity in categorized_entities[domain]
                        ]
                    else:
                        # Include entity IDs and their states for other domains
                        response["Available Home Automation objects"][header] = [
                            {entity.entity_id: entity.state} for entity in categorized_entities[domain]
                        ]
                else:
                    response["Available Home Automation objects"][header] = []

        # Return the formatted JSON to the LLM
        return json.dumps(response)

