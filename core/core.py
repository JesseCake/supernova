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
        self.current_conversation = None
        self.tools = tools.tools

        # Home assistant integration
        self.ha_key = self.get_ha_key()
        self.ha_url = 'http://192.168.20.3:8123/api'
        self.home_assistant = HAClient(self.ha_url, self.ha_key)

        self.available_functions = {
            'get_current_time': self.get_current_time,
            'web_search': self.web_search,
            'open_web_link': self.open_web_link,
            'wikipedia_search': self.wikipedia_search,
            'ha_set_switch': self.ha_set_switch,
            'ha_activate_scene': self.ha_activate_scene
        }

    def create_session(self, session_id):
        print(f'Creating new session with ID: {session_id}')
        # create a new session for each connection inbound to keep histories etc separate:
        self.sessions[session_id] = {
            'conversation_history': [],
            'response_queue': queue.Queue(),
            'response_finished': threading.Event()
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

    def add_ha_to_pre_context(self):
        """Adds the available Home Assistant connections to the pre-context"""
        new_context_info = self.ha_get_available_switches_and_scenes()
        self.pre_context += new_context_info

    def process_input(self, input_text, session_id, voice=False):
        #print("STARTING NEW INPUT")

        # Retrieve the session-specific data:
        session = self.get_session(session_id)
        if session is None:
            self.create_session(session_id)
            session = self.get_session(session_id)

        # clear the response_finished event at the start:
        session['response_finished'].clear()

        conversation_history = session['conversation_history']
        response_queue = session['response_queue']

        # if we're starting a new conversation, create the pre-context and instructions:
        if conversation_history is None:
            conversation_history = []

        prompt = self.create_prompt(
            input_text=input_text,
            conversation_history=conversation_history,
        )

        conversation_history.append({
            'role': 'user',
            'content': input_text,
        })

        # Debugging: Print the formatted conversation context:
        # print(f"Generated prompt: {prompt}")

        while True:
            full_response = None
            tool_calls = None

            full_response, tool_calls = self.send_to_ollama(prompt, session)

            if full_response:
                conversation_history.append({
                    'role': 'assistant',
                    'content': full_response
                })

            if tool_calls:
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

                # update the prompt for next spin around:
                prompt = self.update_prompt(conversation_history)
            else:
                break

        # if we break out of loop, set that we've finished to other threads:
        self.send_whole_response("", session)
        session['response_finished'].set()

        # print('\ncore: Finishing processing input and response')

    def create_prompt(self, input_text, conversation_history, functions_json=None):
        loadprecontext = importlib.import_module('config.precontext')
        importlib.reload(loadprecontext)

        self.pre_context = loadprecontext.llama3_context
        self.add_ha_to_pre_context()

        system_section = {
            'role': 'system',
            'content': self.pre_context,
        }

        history_section = conversation_history

        user_input_section = {
            'role': 'user',
            'content': input_text
        }

        prompt = [system_section] + history_section + [user_input_section]

        if functions_json:
            tools_section = functions_json
            prompt.insert(1, tools_section)

        return prompt

    def update_prompt(self, conversation_history):
        loadprecontext = importlib.import_module('config.precontext')
        importlib.reload(loadprecontext)

        self.pre_context = loadprecontext.llama3_context
        self.add_ha_to_pre_context()

        system_section = {
            'role': 'system',
            'content': self.pre_context,
        }

        history_section = conversation_history

        prompt = [system_section] + history_section

        return prompt

    def send_to_ollama(self, prompt_text, session):
        """Sends request to Ollama, processes return along with tool calls, streams response to message queue"""
        response_queue = session['response_queue']

        try:
            response_stream = self.ollama_client.chat(
                model=self.model,
                messages=prompt_text,
                stream=True,
                keep_alive="2h",
                tools=self.tools
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
                        print(f'{response_content}', end='')
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
        # self.close_channel()

    def get_current_time(self, tool_args, session):
        self.send_whole_response("Checking Time", session)
        now = datetime.now()
        now_time = now.strftime('%I:%M%p')
        return json.dumps({'current_time': now_time})

    def web_search(self, tool_args, session):
        query = tool_args.get('parameters').get('query')
        self.send_whole_response(f"Performing Web Search: '{query}'", session)

        try:
            # Using the google search function to get results
            results = []
            for url in search(query, num=10, stop=10, pause=2):
                results.append({'link': url})

            if not results:
                results.append({'error': 'no results found, probably web search tool failure'})

        except Exception as e:
            return json.dumps({'web_search_error': f'Error in web search: {e}'})

        return json.dumps({'web_search_results': results})

    def open_web_link(self, tool_args, session, max_retries=3):
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
        self.send_whole_response("Opened Website", session)
        return json.dumps({'web_link_error': f'Failed to open web link after {max_retries} attempts'})

    def wikipedia_search(self, tool_args, session):
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

        return json.dumps({'wikipedia_search_results': f'{results}'})

    def ha_set_switch(self, tool_args, session):
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

