import numpy as np
import pyaudio
import logging
import ollama
import os
import time
import json
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel
import simpleaudio as sa
from TTS.api import TTS
import functions
import re

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
        self.pre_context = self.load_pre_context('precontext_llama3.txt')
        self.current_conversation = None

        # Preload sounds
        self.sounds_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sounds')
        self.open_channel_sound = sa.WaveObject.from_wave_file(
            os.path.join(self.sounds_folder, "channel_open.wav")
        )
        self.close_channel_sound = sa.WaveObject.from_wave_file(
            os.path.join(self.sounds_folder, "channel_closed.wav")
        )

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
                #print("Raw content from file:")
                #print(pre_context)

                # Clean the pre-context to replace escaped characters
                pre_context = self.clean_text(pre_context)

                # Print cleaned content for debugging
                #print("Cleaned content:")
                #print(pre_context)

                print(f"Loaded pre-context from {file_path}")
                return pre_context
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
            logging.info(f"Transcribing {self.frames_np.size} samples")
            try:
                segments, _ = self.transcriber.transcribe(self.frames_np)
                if segments:
                    result_text = " ".join([segment.text for segment in segments])
                    # logging.info(f"Transcription result: {result_text}")
                    if not self.channel_open and self.wake_word in result_text.lower():
                        print(f"Wake word detected: {self.wake_word}")
                        self.channel_open = True
                        self.play_open_channel_sound()
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
        print(f"Generated prompt: {prompt}")

        # Send the text to Ollama:
        #raw_response, function_calls, error = self.send_to_ollama(prompt)
        raw_response = self.send_to_ollama(prompt)

        """if error:
            print(f"Error from Ollama: {error}")
            return

        if function_calls:
            fn_responses = self.call_functions(function_calls)
            # self.current_conversation += f"\n[RESPONSE] {json.dumps(fn_responses)} [/RESPONSE]"
            # Ensure that fn_responses is a list of strings
            #self.current_conversation.append(f"[ASSISTANT] {json.dumps(fn_responses)}")
            self.handle_response(json.dumps(fn_responses))"""
        if raw_response:
            #print(f"Chunking text to speech synth: {raw_response}")
            # self.current_conversation += f"\n[RESPONSE] {raw_response} [/RESPONSE]"
            #self.current_conversation.append(f"[ASSISTANT] {raw_response}")
            # self.handle_response(raw_response)
            self.current_conversation.append({
                'role': 'assistant',
                'content': raw_response
            })

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

            if self.chat_mode is False:  # we're using the generate mode
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
                formatted_prompt = json.dumps(prompt_text, indent=2)
                print("Formatted prompt being send:")
                print(formatted_prompt)

                response_stream = self.ollama_client.chat(  # alternative is generate
                    model=self.model,
                    messages=prompt_text,
                    stream=True,
                    keep_alive="2h",
                    # raw=True,
                )

            # Initialise empty containers for response and function calls
            tool_calls = []
            final_response = ""
            accumulated_text = ""

            # Define sentence-ending punctuation
            sentence_endings = re.compile(r'([.,!?])')
            end_conversation = re.compile(r'\[end\]', re.IGNORECASE)

            # iterate over the response stream as it comes in:
            for chunk in response_stream:
                # debugging:
                print(f"Raw response chunk: {chunk}")

                response_content = chunk.get('message', {}).get('content', '')

                # debugging:
                # print(f"{response_content}")

                if response_content:
                    if end_conversation.search(accumulated_text):
                        # we've found an end marker, speak the rest of text then return
                        cleaned_text = end_conversation.sub('', accumulated_text)
                        self.speak_text(accumulated_text)
                        self.current_conversation = None
                        self.play_close_channel_sound()
                        return None

                    else:
                        accumulated_text += response_content
                        final_response += response_content

                        # Check if accumulated text ends with a sentence-ending punctuation
                        if sentence_endings.search(accumulated_text):
                            # Speak the sentence
                            self.speak_text(accumulated_text)
                            # wipe it out fresh after speaking
                            accumulated_text = ""

            # Speak any remaining text after processing all chunks
            #if accumulated_text.strip():
            #    self.speak_text(accumulated_text.strip())

            # Clean up the final response
            final_response = final_response.strip()

            print("FINISHED RESPONSE")

            return final_response

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
        # Split the response into segments by both periods and commas, this keeps things nimble and fast speaking while generating
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

    def play_open_channel_sound(self):
        self.play_sound(self.open_channel_sound)

    def play_close_channel_sound(self):
        self.play_sound(self.close_channel_sound)

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
