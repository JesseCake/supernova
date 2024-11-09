import uuid
import numpy as np
import websocket
import json
import time
import threading
import logging
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel
from TTS.api import TTS

# Setup logging
logging.basicConfig(level=logging.INFO)


class ESPHomeVoiceInterface:
    def __init__(self, core_processor, esphome_url, model_path='tiny.en', use_vad=True):
        self.core_processor = core_processor
        self.session_id = None

        # audio properties tuning:
        self.RATE = 16000
        self.CHUNK = 4096

        # Voice Activity Detection and Transcriber
        self.model_path = model_path
        self.use_vad = use_vad
        self.vad_detector = VoiceActivityDetector(frame_rate=self.RATE)
        self.transcriber = WhisperModel(model_path)
        self.frames_np = np.array([], dtype=np.float32)

        # TTS setup
        self.tts_model = "tts_models/en/vctk/vits"
        self.tts = TTS(self.tts_model, progress_bar=False)
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'

        # ESPHome Wyoming protocol setup
        self.esphome_url = esphome_url
        self.ws = websocket.WebSocketApp(esphome_url,
                                         on_message=self.on_message,
                                         on_open=self.on_open,
                                         on_close=self.on_close)

        # Communication flags
        self.speaking = False
        self.recording = False
        self.channel_open = False

    def on_open(self, ws):
        logging.info("Connected to ESPHome device.")

    def on_close(self, ws, close_status_code, close_msg):
        logging.info("Disconnected from ESPHome device.")

    def on_message(self, ws, message):
        audio_data = np.frombuffer(message, dtype=np.float32)
        if self.use_vad and self.vad_detector(audio_data):
            self.recording = True
            self.add_frames(audio_data)
        elif self.recording:
            self.transcribe_audio()
            self.recording = False

    def add_frames(self, frame_np):
        self.frames_np = np.concatenate((self.frames_np, frame_np),
                                        axis=0) if self.frames_np.size > 0 else frame_np.copy()

    def contact_core(self, input_text):
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)

        process_thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": self.session_id, "is_voice": True}
        )
        process_thread.start()

        session = self.core_processor.get_session(self.session_id)
        assistant_response = ""

        while True:
            if not session['response_queue'].empty():
                response_chunk = session['response_queue'].get()
                if response_chunk is None:
                    break
                else:
                    assistant_response += response_chunk

        if assistant_response:
            self.speak_text(assistant_response.strip())

    def transcribe_audio(self):
        if self.frames_np.size > 0:
            segments, _ = self.transcriber.transcribe(self.frames_np)
            if segments:
                result_text = " ".join([segment.text for segment in segments])
                if not self.channel_open:
                    self.channel_open = True
                    self.open_channel()
                else:
                    close_channel = self.contact_core(result_text)
                    if close_channel:
                        self.close_channel()
                    else:
                        self.generate_tone(300, 0.1, 0.2)
            self.frames_np = np.array([], dtype=np.float32)

    def speak_text(self, text):
        audio = self.tts.tts(text, speed=self.speech_speed, speaker=self.speech_speaker)
        audio_data = np.array(audio, dtype=np.float32).tobytes()
        self.ws.send(audio_data)

    def open_channel(self):
        self.speak_text("I'm here")

    def close_channel(self):
        self.channel_open = False
        for _ in range(3):
            self.generate_tone(300, 0.2, 0.2)
            time.sleep(0.3)

    def generate_tone(self, frequency=440, duration=0.2, volume=0.5):
        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(frequency * t * 2 * np.pi) * volume
        audio = np.int16(tone * (2 ** 15 - 1)).tobytes()
        self.ws.send(audio)

    def run(self):
        self.ws.run_forever()

    def cleanup(self):
        self.ws.close()
