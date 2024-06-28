import numpy as np
import pyaudio
import logging
import ollama
import os
import time
import json

import precontext
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


# Setup logging
logging.basicConfig(level=logging.INFO)


class IntegratedTranscription:
    RATE = 16000
    CHUNK = 4096

    def __init__(self, model_path='tiny.en', use_vad=True):

        self.chat_mode = True  # for using chat endpoint for Ollama or not
        # self.model = "mistral:instruct"
        self.model = "llama3"
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
        self.ollama_client = ollama.Client()
        # self.preload_ollama()

        print("Finished Initialization: READY")
        self.speak_text("Finished starting up")

    def duckduckgo_search(self, query):
        """Perform a web search using DuckDuckGo and return a list of results."""
        print(f"SEARCHING TEXT: {query}")
        try:
            encoded_query = urllib.parse.quote(query)
            url = f"https://duckduckgo.com/html/?q={encoded_query}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }

            response = requests.get(url, headers=headers)
            soup = BeautifulSoup(response.text, 'html.parser')

            results = []
            for result in soup.find_all('a', class_='result__a'):
                title = result.text
                link = result['href']
                results.append({'title': title, 'link': link})
        except Exception as e:
            print(f"WEB SEARCH ERROR: {e}")
            return "Function return: error in web search module"

        return results

    def open_web_link(self, url):
        """Open a web link and return the text content."""
        session = HTMLSession()
        response = session.get(url)
        response.html.render()

        soup = BeautifulSoup(response.html.html, 'html.parser')
        return soup.get_text()

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
        context.append({'role': 'system', 'content': text})
        return context

    def process_transcription(self, transcribed_text):
        # if we're starting a new conversation, create the pre-context and instructions:
        if self.current_conversation is None:
            print("STARTING NEW CONVERSATION")
            self.current_conversation = []

        if self.model == "llama3":
            prompt = self.create_prompt(
                transcribed_text=transcribed_text,
                conversation_history=self.current_conversation,
                # functions_json=functions.functions_json,  # add this back in when we figure out functions
            )

        # Append the new user input to the conversation history (but not before the prompt:
        # self.current_conversation.append(f"[USER] {transcribed_text}")  # raw method
        self.current_conversation.append({
            'role': 'user',
            'content': transcribed_text,
        })

        # Debugging: Print the formatted conversation context:
        # print(f"Generated prompt: {prompt}")

        while True:  # we will stay in a loop for function callbacks unless broken out
            # Send the text to Ollama:
            full_response, command = self.send_to_ollama(prompt)

            if full_response:
                self.current_conversation.append({
                    'role': 'assistant',
                    'content': full_response
                })

            # if we have a command response
            if command:  # when we have an actual response
                self.generate_tone(700, 0.05, 0.2)
                # debugging for command responses
                # print(f"Command type: {type(command)}")

                if isinstance(command, str):
                    try:
                        command = json.loads(command)
                        print(f"Parsed command: {command}")
                    except json.JSONDecodeError as e:
                        print(f"Failed to parse command JSON: {e}")
                        command = {}

                function_name = command.get("function")
                print(f"Command: {function_name}")

                if function_name == "end_conversation":
                    print("RECEIVED END COMMAND")
                    # wipe out our conversation history and set flag for sleep
                    self.close_channel()
                    break
                elif function_name == "get_current_time":
                    print("RECEIVED TIME COMMAND")
                    """Get the current time in a simple 12-hour format."""
                    now = datetime.now()
                    nowtime = now.strftime('%I:%M%p')
                    self.current_conversation.append({
                        'role': 'system',
                        'content': f"function return: {nowtime}"
                    })
                    print(f"Added time {nowtime} to history")
                elif function_name == "web_search":
                    query = command.get("query")
                    if query:
                        results = self.duckduckgo_search(query)
                        self.current_conversation.append({
                            'role': 'user',
                            'content': f"search results: {results}"
                        })
                        self.speak_text("Processing search results.")
                elif function_name == "open_web_link":
                    url = command.get("url")
                    if url:
                        page_text = self.open_web_link(url)
                        self.current_conversation = self.add_to_context(page_text, self.current_conversation)
                        self.speak_text("Content added to context.")
                else:
                    print("UNKNOWN COMMAND")
                    self.current_conversation.append({
                        'role': 'user',
                        'content': "Unknown command or incorrect command format, try again"
                    })

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
                print("Formatted prompt being send:")
                for item in prompt_text:
                    print(f"{item['role']}: {item['content']}\n")

                response_stream = self.ollama_client.chat(  # alternative is generate
                    model=self.model,
                    messages=prompt_text,
                    stream=True,
                    keep_alive="2h",
                    # raw=True,
                )

            # Initialise empty containers for response and function calls

            # for the command logic:
            command_data = None
            json_accumulator = ""
            json_collecting = False

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
                print(f".", end='')

                response_content = chunk.get('message', {}).get('content', '')

                # add the full response to an accumulator to add to conversation history:
                full_response += response_content

                # debugging:
                # print(f"{response_content}")

                if response_content:

                    # watch for JSON and accumulate if so:
                    if '{' in response_content:
                        json_collecting = True

                    if json_collecting:
                        json_accumulator += response_content

                    if '}' in response_content and json_collecting:
                        try:
                            # Attempt to parse the JSON from the accumulated text
                            response_json = json.loads(json_accumulator.strip())
                            print(f"Response JSON: {response_json}")

                            # Check for the function key in the parsed JSON
                            if "function" in response_json:
                                command_data = response_json
                                print(f"GOT JSON: {command_data}")
                                break
                        except json.JSONDecodeError:
                            # print("NOT JSON")
                            # If it's not a complete JSON object yet, keep accumulating
                            pass

                    if not json_collecting:
                        # accumulation for speaking text
                        accumulated_text += response_content

                        # Check if accumulated text ends with a sentence-ending punctuation
                        if sentence_endings.search(accumulated_text):
                            # Speak the sentence
                            self.speak_text(accumulated_text)
                            # wipe it out fresh after speaking
                            accumulated_text = ""

            # Speak any remaining text after processing all chunks (if there is any)
            if not accumulated_text == "":
                self.speak_text(accumulated_text.strip())

            print("FINISHED RESPONSE")

            return full_response, command_data

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
        self.generate_tone(300, 0.2, 0.2)

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
