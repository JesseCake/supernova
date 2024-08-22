import numpy as np
import pyaudio
import logging
import ollama
import os
import time
import json

import precontext
import tools
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel
import simpleaudio as sa
from TTS.api import TTS
import functions
import re
from datetime import datetime

import importlib  # for updating the pre-context live

# for the web request/search sections:
import requests
from bs4 import BeautifulSoup
from requests_html import HTMLSession
import urllib.parse

# wikipedia search
import wikipedia

# for database interaction:
import sqlite3

# home assistant API link
from homeassistant_api import Client as HAClient


# Setup logging
logging.basicConfig(level=logging.INFO)


class IntegratedTranscription:
    RATE = 16000
    CHUNK = 4096

    def __init__(self, model_path='tiny.en', use_vad=True):

        self.chat_mode = True  # for using chat endpoint for Ollama or not
        # self.model = "mistral:instruct"
        self.model = "llama3.1"  # we are still using llama3, but have modded params in a new modelfile
        # self.model = "dolphin-llama3:8b"
        # self.model = "supernova"
        self.model_path = model_path
        self.use_vad = use_vad
        self.vad_detector = VoiceActivityDetector(frame_rate=self.RATE)
        self.transcriber = WhisperModel(model_path)
        self.frames_np = np.array([], dtype=np.float32)

        # TTS setup
        self.tts_model = "tts_models/en/vctk/vits"
        self.tts = TTS(self.tts_model, progress_bar=False)
        self.sample_rate = 22050
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'
        self.p = pyaudio.PyAudio()

        self.paused = False
        self.speaking = False
        self.recording = False
        self.setup_audio_stream()
        self.wake_word = "supernova"
        self.close_channel_phrase = "finish conversation"
        self.channel_open = False
        # self.pre_context = self.load_pre_context('precontext_llama3.txt')
        self.pre_context = precontext.llama3_context
        self.tools = tools.tools
        self.current_conversation = None

        # Preload sounds (not using right now, opting for spoken responses for now)
        '''self.sounds_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sounds')
        self.open_channel_sound = sa.WaveObject.from_wave_file(
            os.path.join(self.sounds_folder, "channel_open.wav")
        )
        self.close_channel_sound = sa.WaveObject.from_wave_file(
            os.path.join(self.sounds_folder, "channel_closed.wav")
        )'''

        # Define the available functions in a dictionary
        self.available_functions = {
            functions.end_conversation.__name__: functions.end_conversation,
            functions.get_current_weather.__name__: functions.get_current_weather,
            functions.get_current_time.__name__: functions.get_current_time,
        }
        self.ollama_client = ollama.Client(host='http://192.168.20.200:11434')
        # self.preload_ollama()

        # init the db if it needs it:
        self.init_db()

        # Home assistant API key:
        self.ha_key = self.get_ha_key()
        self.ha_url = 'http://192.168.20.3:8123/api'
        self.home_assistant = HAClient(self.ha_url, self.ha_key)

        # NOW LET's ADD AVAILABLE HA STUFF TO THE PRE-CONTEXT:
        self.add_ha_to_pre_context()

        # test
        #self.ha_get_switches()
        #self.ha_set_switch('switch.espresso', 'on')
        #print(self.ha_list_entities_with_states())
        #self.ha_set_scene('bedroom_bright_light')
        #print(self.ha_get_available_switches_and_scenes())


        print("Finished Initialization: READY")
        self.speak_text("Finished starting up")

    def get_ha_key(self):
        """
        Retrieves your API key you've set up in Home Assistant and stored in a file 'home_assistant_api'
        Make sure you put in that file only:
        HA_API_KEY = "yourkeyhere"
        """
        with open("home_assistant_api", "r") as file:
            # Iterate over each line in the file
            for line in file:
                # Check if the line starts with "HA_API_KEY"
                if line.startswith("HA_API_KEY"):
                    # Split the line at the '=' and strip any whitespace and quotes
                    return line.split('=')[1].strip().strip('"')

    def add_ha_to_pre_context(self):
        """adds our home assistant available entities to the end of the pre-context"""
        # Get the formatted list of available switches and scenes
        new_context_info = self.ha_get_available_switches_and_scenes()

        # Append this new information to the existing pre-context
        self.pre_context += new_context_info

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

    def ha_set_switch(self, tool_args):
        entity_id = tool_args.get('parameters').get('entity_id')
        state = tool_args.get('parameters').get('state')

        print(f"TOOL: SET SWITCH entity_id={entity_id}, state={state}")

        # Validate the desired state
        if state not in ["on", "off"]:
            print(f"Bad state: {state}")
            return json.dumps({"setting switch error":"State must be either 'on' or 'off'."})

        print('GETTING DOMAIN')
        # Get the switch domain
        switch = self.home_assistant.get_domain("switch")

        '''print('GETTING ALL SWITCHES FROM DOMAIN')
        # Get the list of all available switch entity_ids
        all_switches = [switch.entity_id for switch in switch.get_entities()]

        print('CHECKING IF ID IS IN AVAILABLE SWITCHES')
        # Check if the provided entity_id is in the list of available switches
        if entity_id not in all_switches:
            print("HA set switch: Bad switch id")
            return json.dumps({
                "set switch error": f"Invalid switch ID '{entity_id}'.",
                "available switches": all_switches,
                "instruction": "Try again with valid ID"
            })'''
        print('getting to switching section')
        try:
            # Call the appropriate service based on the state
            if state == "on":
                switch.turn_on(entity_id=entity_id)
            else:
                switch.turn_off(entity_id=entity_id)

            print(f"Successful switch!")
            return json.dumps({'set switch': f'successfully switched {entity_id} {state}'})

        except Exception as e:
            print(f"Failed to call switch")
            return json.dumps({'set switch error': f'Error in switching {entity_id} {state}'})

    def ha_activate_scene(self, tool_args):
        scene_id = tool_args.get('parameters').get('scene_id')
        # Get the scene domain
        scene = self.home_assistant.get_domain("scene")

        # Construct the full entity_id for the scene
        scene_id = f"scene.{scene_id}"

        try:
            # Attempt to activate the scene
            scene.turn_on(entity_id=scene_id)
            # Return success message
            return json.dumps({'activate scene': f'Successfully activated {scene_id}'})
        except Exception as e:
            # Return error message if something goes wrong
            return json.dumps({'activate scene error': f'Failed to activate {scene_id}: {str(e)}'})


    def init_db(self, db_name="knowledge.db"):
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY,
                title TEXT UNIQUE,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def search_knowledge_exact(self, term, db_name="knowledge.db"):
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT title, content FROM knowledge WHERE title = ?", (term,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def search_knowledge_wildcard(self, term, db_name="knowledge.db"):
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT title, content FROM knowledge WHERE title LIKE ?", (f"%{term}%",))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def search_knowledge_partial(self, term, db_name="knowledge.db"):
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT title, content FROM knowledge WHERE title LIKE ?", (f"{term}%",))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def process_knowledge_search_command(self, command):
        function_name = command.get("function")
        term = command.get("term")
        if function_name == "search_knowledge_exact":
            results = self.search_knowledge_exact(term)
        elif function_name == "search_knowledge_wildcard":
            results = self.search_knowledge_wildcard(term)
        elif function_name == "search_knowledge_partial":
            results = self.search_knowledge_partial(term)
        else:
            results = []

        if results:
            result_texts = [f"Title: {title}, Content: {content}" for title, content in results]
            result_text = "\n".join(result_texts)
            self.speak_text(f"Found knowledge")
            return result_text
        else:
            self.speak_text("No found knowledge.")
            return f"Function result: No matching knowledge found"

    def list_knowledge_titles(self, db_name="knowledge.db"):
        """
        Lists all the titles stored in the knowledge database.

        :param db_name: The name of the database file.
        :return: A list of all titles in the knowledge database.
        """
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM knowledge")
        rows = cursor.fetchall()
        conn.close()
        result_text = "Function return: Titles in Knowledgebase:"
        if rows:
            result_texts = [f"ID: {row[0]}, Title: {row[1]}" for row in rows]
            result_text = "\n".join(result_texts)
            self.speak_text("Checked Knowledgebase.")
            return result_text
        else:
            return "Function return: nothing in knowledgebase!"

    def store_knowledge(self, title, content, db_name="knowledge.db"):
        """
        Stores knowledge in the database. If the title already exists, updates the content and timestamp.

        :param title: The title of the knowledge entry.
        :param content: The content of the knowledge entry.
        :param db_name: The name of the database file.
        """
        try:
            conn = sqlite3.connect(db_name)
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO knowledge (title, content) VALUES (?, ?)", (title, content))
                conn.commit()
            except sqlite3.IntegrityError:
                cursor.execute("UPDATE knowledge SET content = ?, timestamp = CURRENT_TIMESTAMP WHERE title = ?",
                               (content, title))
                conn.commit()
            conn.close()
            return "Function return: Knowledge stored successfully"
        except Exception as e:
            return f"Function return: Failed to store knowledge: {e}"

    def delete_knowledge(self, row_number, db_name="knowledge.db"):
        """
        Deletes a knowledge entry from the database based on its row number.

        :param row_number: The row number of the knowledge entry to delete.
        :param db_name: The name of the database file.
        :return: A message indicating whether the deletion was successful or if the entry was not found.
        """
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()

        # Fetch the row to get the ID
        cursor.execute("SELECT id FROM knowledge LIMIT 1 OFFSET ?", (row_number - 1,))
        row = cursor.fetchone()

        if row:
            id_to_delete = row[0]
            cursor.execute("DELETE FROM knowledge WHERE id = ?", (id_to_delete,))
            conn.commit()
            rows_deleted = cursor.rowcount
            conn.close()
            if rows_deleted > 0:
                return f"Function return: Knowledge entry in row number '{row_number}' was deleted."
            else:
                return f"Function return: Failed to delete the knowledge entry in row number '{row_number}'."
        else:
            conn.close()
            return f"Function Return: No knowledge entry found in row number '{row_number}'."

    def web_search(self, tool_args):
        """Perform a web search using DuckDuckGo and return a list of results."""
        query = tool_args.get('parameters').get('query')
        print(f'TOOL: WEB SEARCH "{query}"')

        try:
            encoded_query = urllib.parse.quote(query)
            # debugging:
            # print(f"Encoded query = {encoded_query}")
            url = f"https://duckduckgo.com/html/?q={encoded_query}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            }

            session = requests.Session()
            response = session.get(url, headers=headers, timeout=50, allow_redirects=True)

            # debug:
            #print(f'RAW RESPONSE: {response.text}')

            if response.status_code != 200:
                print(f"Non-200 response: {response.status_code}")
                return json.dumps({'web_search_error': f'error in web search module. Error: {response.status_code}'})

            soup = BeautifulSoup(response.text, 'html.parser')

            #results = [{'Function Return': "Use the following search results to comprehend and summarize, and use the web links with the open web link function for deeper information to do the same. Do not just read out the web links themselves."}]
            results = []
            for result in soup.find_all('a', class_='result__a'):
                title = result.text
                link = result['href']
                results.append({'title': title, 'link': link})

            if not results:
                print("No results returned, failure most probably")
                results.append({'error': 'no results found, probably web search tool failure'})

        except requests.RequestException as e:
            print(f"WEB SEARCH ERROR: {e}")
            #return f"Function return: error in web search module: {e}. Decide how to proceed."
            return json.dumps({'web_search_error': f'Error in web search: {e}'})
        except Exception as e:
            print(f"WEB SEARCH ERROR: {e}")
            #return f"Function return: error in web search module: {e}. Decide how to proceed."
            return json.dumps({'web_search_error': f'Error in web search: {e}'})

        return json.dumps({'web_search_results': f'{results}'})

    def open_web_link(self, tool_args, max_retries=3):
        """Open a web link and return the text content."""
        print("TOOL: OPEN WEB LINK")
        session = HTMLSession()
        url = tool_args.get('parameters').get('url')

        for attempt in range(max_retries):
            try:
                response = session.get(url)
                response.html.render()
                soup = BeautifulSoup(response.html.html, 'html.parser')
                #return soup.get_text()
                return json.dumps({'web_link_results': soup.get_text()})
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.ConnectionError) and 'Name or service not known' in str(e):
                    print(f"DNS resolution error: {e}")
                    #return f"Function return: DNS resolution error for {url}"
                    return json.dumps({'web_link_error': f"DNS resolution error for {url}"})
                print(f"Attempt {attempt + 1} failed: {e}")
                time.sleep(2)  # Wait for 2 seconds before retrying
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
                #return f"Function return: unexpected error for {url}"
                return json.dumps({'web_link_error': f'Unexpected error for {url}: {e}'})
        return json.dumps({'web_link_error': f'Failed to open web link after {max_retries} attempts'})

    def wikipedia_search(self, tool_args):
        """Search Wikipedia for results"""
        query = tool_args.get('parameters').get('query')

        print("Wikipedia searching...")
        search_results = wikipedia.search(query)
        print("Wikipedia got results")

        results = []

        if search_results:
            for title in search_results:
                print(f"Retrieving info for result title: {title}")
                try:
                    summary = wikipedia.summary(title, sentences=2)
                    page = wikipedia.page(title)

                    soup = BeautifulSoup(page.html(), features="lxml")

                    result = {
                        "title": title,
                        "summary": summary,
                        "url": page.url
                    }
                    results.append(result)
                except wikipedia.DisambiguationError as e:
                    # Handle disambiguation pages in necessary
                    results.append({
                        "title": title,
                        "summary": "Disambiguation page, multiple meanings exist",
                        "url": None
                    })
                except wikipedia.PageError:
                    # Handle page not found errors
                    results.append({
                        "title": title,
                        "summary": "Page does not exist.",
                        "url": None
                    })
        else:
            results.append('No results, try another search term')

        return json.dumps({'wikipedia_search_results': f'{results}'})

    def get_current_time(self, tool_args):
        print("TOOL: GET CURRENT TIME")
        """Get the current time in a simple 12-hour format."""
        now = datetime.now()
        nowtime = now.strftime('%I:%M%p')
        return json.dumps({'current_time': nowtime})

    def end_conversation(self, tool_args):
        print("TOOL: END CONVERSATION")
        self.close_channel()

    def clean_text(self, text):
        # Replace common escaped characters with their actual meanings
        text = text.replace('\\n', '\n').replace('\\t', '\t').replace('\\\'', '\'')
        text = text.replace('\\\\"', '\"')  # Added handling for escaped quotes
        return text

    def load_pre_context(self, filename):
        # shortened context debugging:
        # return "You are a helpful assistant that answers questions in a short manner"
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                pre_context = file.read().strip()
                # Print raw content for debugging
                print("Raw content from file:")
                print(pre_context)

                # Clean the pre-context to replace escaped characters
                pre_context = self.clean_text(pre_context)

                print(f"Loaded pre-context from {file_path}")
                return f"{pre_context}"
        except FileNotFoundError:
            logging.error(f"Pre-context file not found: {file_path}")
            return "You are a helpful assistant."

    def setup_audio_stream(self):
        try:
            self.stream = self.p.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK
            )
        except OSError as error:
            logging.error(f"Unable to access microphone: {error}")
            self.stream = None

    def add_frames(self, frame_np):
        self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0) if self.frames_np.size > 0 else frame_np.copy()

    def transcribe_audio(self):
        if self.frames_np.size > 0:
            # print(f"Transcribing {self.frames_np.size} samples")
            try:
                segments, _ = self.transcriber.transcribe(self.frames_np)
                if segments:
                    result_text = " ".join([segment.text for segment in segments])
                    # logging.info(f"Transcription result: {result_text}")
                    if not self.channel_open and self.wake_word in result_text.lower():
                        print(f"Wake word detected: {self.wake_word}")
                        self.channel_open = True
                        self.open_channel()
                    elif self.channel_open:
                        if self.close_channel_phrase in result_text.lower():
                            functions.end_conversation(transcriber)
                        else:
                            self.process_transcription(result_text)
                else:
                    logging.info("No transcription result.")
            except Exception as e:
                logging.error(f"Error during transcription: {e}")
            finally:
                self.frames_np = np.array([], dtype=np.float32)
        else:
            logging.info("No audio data to transcribe")

    def create_prompt_raw(self, transcribed_text, conversation_history, functions_json):
        # creates the raw prompt with the required function definition tags for Mistral
        tools_section = f"[AVAILABLE_TOOLS]{functions_json}[/AVAILABLE_TOOLS]"
        system_section = f"[SYSTEM]{self.pre_context}[/SYSTEM]"
        history_section = "\n".join(conversation_history)
        user_input_section = f"[INST] {transcribed_text} [/INST]"

        prompt = f"{tools_section}\n{system_section}\n{history_section}\n{user_input_section}"
        return prompt

    def create_prompt(self, transcribed_text, conversation_history, functions_json=None):
        # Reload the precontext module to get the latest context (so we can tweak live)
        loadprecontext = importlib.import_module('precontext')
        importlib.reload(loadprecontext)

        # Use the reloaded context
        self.pre_context = loadprecontext.llama3_context
        # add available HA stuff to pre-context tail
        self.add_ha_to_pre_context()

        # creates a list of dictionaries based prompt using ollama's roles
        # (yet to find out how to do function inclusion well)
        system_section = {
            'role': 'system',
            'content': self.pre_context,
            # 'content': self.pre_context.replace('\n', '\\n').replace('"', '\\"'),
        }

        history_section = conversation_history

        user_input_section = {
            'role': 'user',
            'content': transcribed_text
        }

        prompt = [system_section] + history_section + [user_input_section]

        if functions_json:  # if we have the functions section?
            tools_section = functions_json
            prompt.insert(1, tools_section)

        return prompt

    def update_prompt(self, conversation_history):
        # Reload the precontext module to get the latest context (so we can tweak live)
        loadprecontext = importlib.import_module('precontext')
        importlib.reload(loadprecontext)

        # Use the reloaded context
        self.pre_context = loadprecontext.llama3_context
        # add available HA stuff to pre-context tail
        self.add_ha_to_pre_context()

        # creates a list of dictionaries based prompt using ollama's roles
        # (yet to find out how to do function inclusion well)
        system_section = {
            'role': 'system',
            'content': self.pre_context,
            # 'content': self.pre_context.replace('\n', '\\n').replace('"', '\\"'),
        }

        history_section = conversation_history

        prompt = [system_section] + history_section

        return prompt

    def add_to_context(self, text, context):
        """Add the retrieved text to the context."""
        context.append({'role': 'assistant', 'content': text})
        return context

    def process_transcription(self, transcribed_text):
        # if we're starting a new conversation, create the pre-context and instructions:
        if self.current_conversation is None:
            print("STARTING NEW CONVERSATION")
            self.current_conversation = []

        prompt = self.create_prompt(
            transcribed_text=transcribed_text,
            conversation_history=self.current_conversation,
            # functions_json=functions.functions_json,  # add this back in when we figure out functions
        )

        # Append the new user input to the conversation history (but not before the prompt:
        # self.current_conversation.append(f"[USER] {transcribed_text}")  # raw method
        self.current_conversation.append({
            'role': 'assistant',
            'content': transcribed_text,
        })

        # Debugging: Print the formatted conversation context:
        # print(f"Generated prompt: {prompt}")

        while True:  # we will stay in a loop for function callbacks unless broken out
            full_response = None
            tool_calls = None

            # Send the text to Ollama:
            full_response, tool_calls = self.send_to_ollama(prompt)

            if full_response:
                self.current_conversation.append({
                    'role': 'user',
                    'content': full_response
                })

            # if we have a command response
            if tool_calls:  # when we have an actual response
                self.generate_tone(700, 0.05, 0.2)

                available_functions = {
                    'end_conversation': self.end_conversation,
                    'get_current_time': self.get_current_time,
                    'web_search': self.web_search,
                    'open_web_link': self.open_web_link,
                    'wikipedia_search': self.wikipedia_search,
                    'ha_set_switch': self.ha_set_switch,
                    'ha_activate_scene': self.ha_activate_scene
                }

                for tool in tool_calls:
                    try:
                        #tool_name = tool.get("name")
                        print(f"Tool: {tool.get('name')}")

                        function_to_call = available_functions[tool['name']]
                        function_response = function_to_call(tool_args=tool)

                        if self.current_conversation:
                            # add function response to the conversation:
                            self.current_conversation.append(
                                {
                                    'role': 'tool',
                                    'content': function_response
                                }
                            )

                    except Exception as e:
                        # something went wrong:
                        self.current_conversation.append(
                            {
                                'role': 'tool',
                                'content': f'Error with tool, or bad use of tool: {e}',
                            }
                        )

                    '''if tool_name == "end_conversation":
                        print("RECEIVED END COMMAND")
                        # wipe out our conversation history and set flag for sleep
                        self.close_channel()
                        break
                        

                    elif tool_name == "web_search":
                        query = command.get("query")
                        if query:
                            results = self.duckduckgo_search(query)
                            self.current_conversation.append({
                                'role': 'user',
                                'content': f"{results}"
                            })
                            self.speak_text("Searching online.")
                    elif tool_name == "open_web_link":
                        print("GOING TO OPEN WEB LINK NOW")
                        url = command.get("url")
                        if url:
                            page_text = self.open_web_link(url)
                            print(f"Pulled page text: {page_text}")
                            self.current_conversation = self.add_to_context(page_text, self.current_conversation)
                            print(f"Added scraped website to conversation text")
                            self.speak_text("Website pulled.")
                    elif "search_knowledge" in tool_name:
                        self.current_conversation = self.add_to_context(
                            self.process_knowledge_search_command(command),
                            self.current_conversation
                        )
                    elif tool_name == "store_knowledge":
                        self.current_conversation = self.add_to_context(
                            self.store_knowledge(
                                command.get("title"),
                                command.get("content")
                            ),
                            self.current_conversation
                        )
                    elif tool_name == "delete_knowledge":
                        self.current_conversation = self.add_to_context(
                            self.delete_knowledge(
                                command.get("id")
                            ),
                            self.current_conversation
                        )
                    elif tool_name == "list_knowledge_titles":
                        self.current_conversation = self.add_to_context(
                            self.list_knowledge_titles(),
                            self.current_conversation
                        )
                    else:
                        print("UNKNOWN COMMAND")
                        self.current_conversation.append({
                            'role': 'user',
                            'content': "Unknown command or incorrect command format, try again?"
                        })'''

                # update the prompt for next spin:
                prompt = self.update_prompt(self.current_conversation)


            else:
                break

    def preload_ollama(self):
        # Preload the model so that it doesn't take forever for the first request
        try:
            self.ollama_client.chat(
                model=self.model,
                keep_alive="2h",
                messages=[{"role": "user", "content": "this is preloading you"}],
            )
        except Exception as e:
            print(f"Failure pre-loading model {self.model}: {e}")
            self.speak_text("Failure pre-loading model")

    def send_to_ollama(self, prompt_text):
        try:

            if self.chat_mode is False:  # we're using the generate mode - not good for conversation
                # Concatenate messages from create_prompt into a big fat string
                concatenated_prompt = "\n".join([f"{msg['role']}: {msg['content']}" for msg in prompt_text])
                print(f"Concatenated prompt being sent: {concatenated_prompt}")

                # Send request to Ollama API
                options = {
                    # "max_tokens": 150,     # Limit the response length
                    "temperature": float(1.14),    # Adjust randomness
                    "top_p": float(0.14),          # Use top-p sampling
                    "top_k": int(49),
                    "repeat_penalty": float(1.17),  # is this right?
                    # "presence_penalty": 0, # Control repetition
                    # "frequency_penalty": 0,# Control token frequency
                    # "stream": True         # Enable streaming
                }

                response_stream = self.ollama_client.generate(
                    model=self.model,
                    prompt=concatenated_prompt,
                    options=options,
                    stream=True
                )

            elif self.chat_mode is True:
                # debugging weird pre-context:
                #print("Formatted prompt being send:")
                #for item in prompt_text:
                #    print(f"{item['role']}: {item['content']}\n")

                response_stream = self.ollama_client.chat(  # alternative is generate
                    model=self.model,
                    messages=prompt_text,
                    stream=True,
                    keep_alive="2h",
                    # raw=True,
                    tools=self.tools
                )

            # Initialise empty containers for response and function calls

            # for the command logic:
            # command_data = None
            json_accumulator = ""
            json_collecting = False
            # for keeping track of how many open brackets we have when in json collect mode:
            json_brackets = 0
            # tool call accumulation:
            tool_calls = []

            # the total response to add to conversation history (returned):
            full_response = ""

            # storage for accumulating text in chunks and returning to speech synth as we go to save time:
            accumulated_text = ""

            # Define sentence-ending punctuation
            sentence_endings = re.compile(r'([.,!?])')
            # match comma only when not followed by number, and full stop only when not followed by letter or number
            # sentence_endings = re.compile(r'([.!?](?![A-Za-z0-9])|,(?!\d))')
            end_conversation = re.compile(r'\[end\]', re.IGNORECASE)



            # iterate over the response stream as it comes in:
            for chunk in response_stream:
                # debugging:
                # print(f"Raw response chunk: {chunk}")
                #print(f".", end='')

                response_content = chunk.get('message', {}).get('content', '')

                # add the full response to an accumulator to add to conversation history:
                full_response += response_content

                # debugging:
                # print(f"{response_content}")

                if response_content:
                    # watch for JSON and accumulate if so:

                    for char in response_content:
                        if char == '{':
                            if not json_collecting:
                                json_collecting = True
                                json_accumulator = ""
                            json_brackets += 1
                        if json_collecting:
                            json_accumulator += char
                        if char == '}':
                            json_brackets -= 1
                            if json_brackets == 0:
                                try:
                                    response_json = json.loads(json_accumulator.strip())
                                    print(f"Response JSON: {response_json}")
                                    tool_calls.append(response_json)

                                except json.JSONDecodeError:
                                    print("BAD JSON")

                    if not json_collecting:
                        # accumulation for speaking text
                        accumulated_text += response_content

                        # Check if accumulated text ends with a sentence-ending punctuation
                        if sentence_endings.search(accumulated_text):
                            # Speak the sentence early so we feel snappier
                            self.speak_text(accumulated_text)
                            # wipe it out fresh after speaking
                            accumulated_text = ""

            # Speak any remaining text after processing all chunks (if there is any)
            if not accumulated_text == "":
                self.speak_text(accumulated_text.strip())


            print("FINISHED RESPONSE")
            # Generate a tone to signify that the response has finished
            self.generate_tone(300, 0.1, 0.2)

            return full_response, tool_calls

        except Exception as e:
            print(f"Error communicating with Ollama API: {e}")
            return "", [], e

    def call_functions(self, function_calls):
        print(f"FUNCTION CALLED: {function_calls}")
        results = []
        for call in function_calls:
            function_name = call.get('name')
            parameters = call.get('parameters', {})
            function = self.available_functions.get(function_name)
            if function:
                try:
                    # check if it's our special conversation ending function:
                    if function_name == "end_conversation":
                        result = function(self)
                    else:
                        result = function(**parameters)
                    results.append(result)
                except TypeError as e:
                    print(f"Error calling function '{function_name}': {e}")
                    results.append({"error": f"Function '{function_name}' error: {str(e)}"})
            else:
                print(f"Function '{function_name}' not found")
                results.append({"error": f"Function '{function_name}' not available."})
        return results

    def handle_response(self, response):
        # Split the response into segments by both periods and commas,
        # this keeps things nimble and fast speaking while generating
        segments = re.split(r'[.,]', response)
        for segment in segments:
            clean_segment = segment.strip()
            if clean_segment:
                self.speak_text(clean_segment)

    def speak_text(self, text):
        if text.strip():
            self.speaking = True
            self.pause_audio_stream()
            logging.info("Starting speaking")
            try:
                audio = self.tts.tts(text, speed=self.speech_speed, speaker=self.speech_speaker)
                audio_data = np.array(audio, dtype=np.float32)

                stream = self.p.open(
                    format=pyaudio.paFloat32,
                    channels=1,
                    rate=self.sample_rate,
                    output=True,
                    frames_per_buffer=4096
                )

                chunk_size = 1024
                for start in range(0, len(audio_data), chunk_size):
                    end = start + chunk_size
                    stream.write(audio_data[start:end].tobytes())
                    time.sleep(0.01)

                stream.stop_stream()
                stream.close()
            except Exception as e:
                logging.error(f"Error during speaking: {e}")
            finally:
                logging.info("Finished speaking")
                self.resume_audio_stream()
                self.speaking = False
        else:
            logging.info("Skipped speaking due to empty text.")

    def pause_audio_stream(self):
        if self.stream and not self.paused:
            self.stream.stop_stream()
            self.paused = True

    def resume_audio_stream(self):
        if self.stream and self.paused:
            self.stream.start_stream()
            self.paused = False

    def record_audio(self):
        while True:
            if not self.speaking:
                data = self.stream.read(self.CHUNK, exception_on_overflow=False)
                audio_array = np.frombuffer(data, dtype=np.float32)
                if self.use_vad:
                    if self.vad_detector(audio_array):
                        if not self.recording:
                            self.recording = True
                        self.add_frames(audio_array)
                    elif self.recording:
                        self.transcribe_audio()
                        self.recording = False
            time.sleep(0.01)

    def play_sound(self, wave_obj):
        try:
            self.pause_audio_stream()
            play_obj = wave_obj.play()
            play_obj.wait_done()
        except Exception as e:
            logging.error(f"Error playing sound: {e}")
        finally:
            self.resume_audio_stream()

    def open_channel(self):
        # self.play_sound(self.open_channel_sound)
        self.speak_text("I'm here")

    def close_channel(self):
        # self.play_sound(self.close_channel_sound)
        self.current_conversation = None
        self.channel_open = False
        for _ in range (3):
            self.generate_tone(300, 0.2, 0.2)
            time.sleep(0.3)


    def generate_tone(self, frequency=440, duration=0.2, volume=0.5):
        """
        Generate a tone with the specified frequency, duration, and volume.

        :param frequency: Frequency of the tone in Hz (default is 440 Hz, which is A4).
        :param duration: Duration of the tone in seconds (default is 1.0 seconds).
        :param volume: Volume of the tone (0.0 to 1.0, default is 0.5).
        """
        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(frequency * t * 2 * np.pi)
        audio = tone * (2 ** 15 - 1) / np.max(np.abs(tone))
        audio = audio * volume
        audio = audio.astype(np.int16)

        play_obj = sa.play_buffer(audio, 1, 2, sample_rate)
        play_obj.wait_done()

    def run(self):
        try:
            while True:
                self.record_audio()
                time.sleep(0.01)
        except KeyboardInterrupt:
            logging.info("Stopping due to keyboard interrupt.")
        finally:
            self.cleanup()

    def cleanup(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()

if __name__ == "__main__":
    transcriber = IntegratedTranscription(model_path='tiny.en', use_vad=True)
    transcriber.run()
