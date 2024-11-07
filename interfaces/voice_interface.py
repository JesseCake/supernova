import uuid
import numpy as np
import pyaudio
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel
import simpleaudio as sa
from TTS.api import TTS
import re
import time
import threading
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

class VoiceInterface:
    def __init__(self, core_processor, model_path='tiny.en', use_vad=True):
        self.core_processor = core_processor
        self.session_id = None  # store the session ID here

        # audio properties tuning:
        self.RATE = 16000
        self.CHUNK = 4096

        # voice activity detection and transcriber:
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

        # listening/speaking logic:
        self.paused = False
        self.speaking = False
        self.recording = False
        self.setup_audio_stream()
        self.wake_word = "supernova"
        self.close_channel_phrase = "finish conversation"
        self.channel_open = False

        # output stream:
        self.output_stream = self.p.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=4096
        )

    def close_voice_channel(self, tool_args):
        print("TOOL: CLOSE VOICE CHANNEL")
        self.close_channel()

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

    def contact_core(self, input):
        # we will send the text to the core, and handle the return from the core

        # initialise session if not already done:
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)

        # Run the input processing in a separate thread (so we can leave it running and process the return in chunks
        process_thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input, "session_id": self.session_id, "is_voice": True}
        )
        process_thread.start()

        session = self.core_processor.get_session(self.session_id)  # get the session object from the core to interact with
        assistant_response = ""
        # Define sentence-ending punctuation
        sentence_endings = re.compile(r'([.,!?])')

        # Stream response chunks incrementally
        #while not session['response_finished'].is_set():
        while True:
            if not session['response_queue'].empty():
                response_chunk = session['response_queue'].get()
                if response_chunk is None:
                    print('RESPONSE FINISHED')
                    break
                else:
                    # print(f"{response_chunk}", end="")
                    assistant_response += response_chunk

                    if sentence_endings.search(assistant_response):
                        # Speak the sentence early so we feel snappier
                        self.speak_text(assistant_response)
                        # wipe it out fresh after speaking
                        assistant_response = ""

        # After loop, ensure all remaining text is spoken
        if assistant_response:
            self.speak_text(assistant_response.strip())

        if session['close_voice_channel'].is_set():
            print("CLOSING VOICE CHANNEL")
            return True
        else:
            return False

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
                            self.close_channel()
                        else:
                            # we have successful transcription, let's send it to the agent and handle return:
                            close_channel = self.contact_core(result_text)

                            if close_channel:
                                print(f"Closing voice channel")
                                self.close_channel()
                            else:
                                # generate tone to let us know speaking is finished (but conversation continues):
                                self.generate_tone(300, 0.1, 0.2)
                else:
                    logging.info("No transcription result.")
            except Exception as e:
                logging.error(f"Error during transcription: {e}")
            finally:
                self.frames_np = np.array([], dtype=np.float32)
        else:
            logging.info("No audio data to transcribe")

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

                chunk_size = 1024
                for start in range(0, len(audio_data), chunk_size):
                    end = start + chunk_size
                    self.output_stream.write(audio_data[start:end].tobytes())
                    time.sleep(0.01)

                # self.output_stream.stop_stream()
                # self.output_stream.close()
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
            #time.sleep(0.01)

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
                # time.sleep(0.01)
        except KeyboardInterrupt:
            logging.info("Stopping due to keyboard interrupt.")
        finally:
            self.cleanup()

    def cleanup(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()
