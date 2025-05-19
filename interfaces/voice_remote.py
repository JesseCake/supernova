import asyncio
import uuid
import numpy as np

from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.event import Event
from wyoming.audio import AudioStart, AudioChunk, AudioStop

from core.core import CoreProcessor

from TTS.api import TTS
from whisper_live.transcriber import WhisperModel

class VoiceRemoteWyomingHandler(AsyncEventHandler):
    def __init__(self, reader, writer, core_processor):
        super().__init__(reader, writer)
        self.core_processor = core_processor

        # Set up your TTS/ASR models
        self.tts = TTS("tts_models/en/vctk/vits", progress_bar=False)
        self.speech_speaker = 'p376'
        self.speech_speed = 1.0
        self.sample_rate = 22050
        self.transcriber = WhisperModel('tiny.en')

        # Per-session state
        self.session_id = str(uuid.uuid4())
        self.audio_bytes = b""
        self.got_audio_start = False
        self.pcm_sample_rate = 16000
        self.pcm_dtype = np.float32

        # Greet on connect
        asyncio.create_task(self.greet())

    async def greet(self):
        greeting_text = "I'm here"
        greeting_audio = self.tts.tts(greeting_text, speed=self.speech_speed, speaker=self.speech_speaker)
        greeting_audio = np.array(greeting_audio, dtype=np.float32).tobytes()
        await self.write_event(AudioStart(rate=self.sample_rate, width=4, channels=1).event())
        await self.write_event(AudioChunk(rate=self.sample_rate, width=4, channels=1, audio=greeting_audio).event())
        await self.write_event(AudioStop().event())

    async def handle_event(self, event: Event) -> bool:
        # Handle incoming Wyoming events
        if AudioStart.is_type(event.type):
            self.got_audio_start = True
            self.audio_bytes = b""
        elif AudioChunk.is_type(event.type):
            pkt = AudioChunk.from_event(event)
            self.audio_bytes += pkt.audio
        elif AudioStop.is_type(event.type):
            await self.process_audio()
            # Session done, close connection
            return False
        # Continue receiving events
        return True

    async def process_audio(self):
        if not self.got_audio_start or not self.audio_bytes:
            print("[WYOMING] No audio received.")
            return

        # Transcribe
        audio_np = np.frombuffer(self.audio_bytes, dtype=self.pcm_dtype)
        if self.pcm_sample_rate != 16000:
            print("[WYOMING] WARNING: Resampling not implemented, audio may be wrong sample rate.")
        text_segments, _ = self.transcriber.transcribe(audio_np)
        input_text = " ".join([segment.text for segment in text_segments])
        print(f"[WYOMING] Transcribed: {input_text}")

        # LLM
        self.core_processor.create_session(self.session_id)
        self.core_processor.process_input(
            input_text=input_text,
            session_id=self.session_id,
            is_voice=True
        )
        session = self.core_processor.get_session(self.session_id)
        # Gather LLM response
        response_chunks = []
        while True:
            if not session['response_queue'].empty():
                chunk = session['response_queue'].get()
                if chunk is None:
                    break
                response_chunks.append(chunk)
            else:
                await asyncio.sleep(0.05)
        reply_text = ''.join(response_chunks)
        print(f"[WYOMING] LLM reply: {reply_text}")

        # TTS
        reply_audio = self.tts.tts(reply_text, speed=self.speech_speed, speaker=self.speech_speaker)
        #32 bit:
        #reply_audio = np.array(reply_audio, dtype=np.float32).tobytes()
        reply_audio = np.array(reply_audio * 32767, dtype=np.int16).tobytes()

        # Send Wyoming audio reply (in chunks, if you wish)
        await self.write_event(AudioStart(rate=self.sample_rate, width=4, channels=1).event())
        await self.write_event(AudioChunk(rate=self.sample_rate, width=4, channels=1, audio=reply_audio).event())
        await self.write_event(AudioStop().event())

    async def disconnect(self):
        print(f"[WYOMING] Session {self.session_id} finished.\n")
        return await super().disconnect()

# Main server runner
async def main():
    core_processor = CoreProcessor()

    def handler_factory(reader, writer):
        return VoiceRemoteWyomingHandler(reader, writer, core_processor)

    server = AsyncTcpServer(host="0.0.0.0", port=10300)
    await server.start(handler_factory)
    print("Wyoming server listening on port 10300")
    await asyncio.Event().wait()

# Threadable interface if you want to run in a thread
class VoiceRemoteInterface:
    def __init__(self, core_processor, host="0.0.0.0", port=10300):
        self.core_processor = core_processor
        self.host = host
        self.port = port

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run_server())

    async def _run_server(self):
        def handler_factory(reader, writer):
            return VoiceRemoteWyomingHandler(reader, writer, self.core_processor)
        server = AsyncTcpServer(host=self.host, port=self.port)
        await server.start(handler_factory)
        print(f"[VoiceRemote] Wyoming server listening on {self.host}:{self.port}")
        await asyncio.Event().wait()

# To run: asyncio.run(main())
