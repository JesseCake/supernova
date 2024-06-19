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
        self.model = "mistral:instruct"
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
        self.pre_context = self.load_pre_context('precontext_new.txt')
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

        self.preload_ollama()

        print("Finished Initialization: READY")
        self.speak_text("Finished starting up")

    def load_pre_context(self, filename):
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                pre_context = file.read().strip()
            logging.info(f"Loaded pre-context from {file_path}")
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

    def process_transcription(self, transcribed_text):
        # if we're starting a new conversation, create the pre-context and instructions:
        if self.current_conversation is None:
            print("STARTING NEW CONVERSATION")
            self.current_conversation = (
                f"[INST][AVAILABLE_TOOLS]{json.dumps(functions.definitions)}[/AVAILABLE_TOOLS]\n"
                f"[SYSTEM] {self.pre_context} [/SYSTEM][/INST]"
            )

        # Append the new user input to the conversation history:
        self.current_conversation += f"\n[INST] {transcribed_text} [/INST]"

        # Debugging: Print the formatted conversation context:
        print(f"Updated conversation context: {self.current_conversation}")

        # Send the text to Ollama:
        raw_response, function_calls, error = self.send_to_ollama(self.current_conversation)

        if error:
            print(f"Error from Ollama: {error}")
            return

        if function_calls:
            fn_responses = self.call_functions(function_calls)
            self.current_conversation += f"\n[RESPONSE] {json.dumps(fn_responses)} [/RESPONSE]"
            self.handle_response(json.dumps(fn_responses))
        else:
            self.current_conversation += f"\n[RESPONSE] {raw_response} [/RESPONSE]"
            self.handle_response(raw_response)

    def preload_ollama(self):
        # Preload the model so that it doesn't take forever for the first request
        try:
            ollama.generate(
                model=self.model,
                keep_alive="2h",
                prompt="this is preloading you",
            )
        except Exception as e:
            print(f"Failure pre-loading model {self.model}: {e}")
            self.speak_text("Failure pre-loading model")

    def send_to_ollama(self, prompt_text):
        try:
            # Send request to Ollama API
            response = ollama.generate(
                model=self.model,
                prompt=prompt_text,
                raw=True
            )

            # Print the raw response for debugging
            print(f"Raw response from Ollama: {response}")

            # Extract the 'response' field
            raw_response = response.get('response', '')

            # Print the full 'response' content
            print(f"Full response content: {raw_response}")

            tool_calls = []
            final_response = ""

            # Split the response into segments by double newlines
            json_segments = raw_response.split('\n\n')

            for segment in json_segments:
                segment = segment.strip()
                if segment.startswith('[') and segment.endswith(']'):
                    try:
                        # Attempt to parse the segment as JSON
                        parsed_data = json.loads(segment)
                        if isinstance(parsed_data, list):
                            tool_calls.extend(parsed_data)
                        else:
                            print(f"Unexpected JSON format: {parsed_data}")
                    except json.JSONDecodeError as e:
                        print(f"JSON decoding error in tool calls: {e}")
                        print(f"Segment causing error: {segment}")
                else:
                    final_response += segment + " "

            final_response = final_response.strip()

            return final_response, tool_calls, None

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
