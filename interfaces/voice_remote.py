import asyncio
import numpy as np
from wyoming.server import AsyncTcpServer, AsyncEventHandler
from wyoming.event import Event
from wyoming.audio import AudioChunk, AudioStart, AudioStop, AudioFormat
from whisper_live.transcriber import WhisperModel
from TTS.api import TTS
import re
import uuid
from scipy.signal import resample 

class VoiceRemoteInterface(AsyncEventHandler):
    def __init__(self, reader, writer, core_processor, model_path='tiny.en', **kwargs):
        super().__init__(reader, writer, **kwargs)
        self.core_processor = core_processor
        self.transcriber = WhisperModel(model_path)
        self.tts = TTS("tts_models/en/vctk/vits", progress_bar=False)
        self.session_id = None
        self.current_audio = []
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'
        self.tts_sample_rate = 22050  # built into the model so must use
        self.sentence_endings = re.compile(r'(?<=[.!?])\s+')

    async def handle_event(self, event: Event) -> bool:
        print("received packet:", end="")
        if AudioStart.is_type(event.type):
            print("start")
            self.current_audio = []

        elif AudioChunk.is_type(event.type):
            print("chunk")
            # Gather the audio
            if event.payload is not None:
                audio_np = np.frombuffer(event.payload, dtype=np.int16)
                audio_np = audio_np.astype(np.float32) / 32767.0
                self.current_audio.append(audio_np)
            else:
                print("[remotevoice] Warning: received audio chunk with no payload!")

        elif AudioStop.is_type(event.type):
            print("stop")
            if not self.current_audio:
                return
            # Concatenate all audio chunks
            full_audio = np.concatenate(self.current_audio)
            # Run your transcription (ASR) logic
            segments, _ = self.transcriber.transcribe(full_audio)
            result_text = " ".join([segment.text for segment in segments]) if segments else ""
            print(f"[remote] Transcription: {result_text}")

            # use/create session id and run text through LLM logic:
            if self.session_id is None:
                self.session_id = str(uuid.uuid4())
                self.core_processor.create_session(self.session_id)
            self.core_processor.process_input(input_text=result_text, session_id=self.session_id, is_voice=True)
            session = self.core_processor.get_session(self.session_id)

            # We'll stream out sentences as they become available, similar to your old `contact_core`
            buffer = ""
            while True:
                if not session['response_queue'].empty():
                    response_chunk = session['response_queue'].get()
                    if response_chunk is None:
                        break
                    else:
                        buffer += response_chunk
                        sentences = self.sentence_endings.split(buffer)
                        for sent in sentences[:-1]:
                            if sent.strip():
                                await self.speak_text(sent.strip(), writer)
                        buffer = sentences[-1]
                else:
                    # Give the core a bit more time if needed (non-blocking)
                    await asyncio.sleep(0.01)
                    # Optionally, add a timeout here to avoid infinite waits

            if buffer.strip():
                await self.speak_text(buffer.strip(), writer)
        
        else:
            print(f"Event type: {event.type}")

        return True  # return False to close the connection

    async def speak_text(self, text):
        """Generate TTS audio from text and stream to the Wyoming client."""
        if text.strip():
            # Generate audio using your TTS engine
            audio = self.tts.tts(text, speed=self.speech_speed, speaker=self.speech_speaker)
            audio_data = np.array(audio, dtype=np.float32)

            # Resample to 16kHz
            duration = len(audio_np) / 22050  # the default samplerate of the tts
            num_samples = int(duration * 16000)
            audio_np = resample(audio_np, num_samples)

            # Convert to int16 bit
            audio_int16 = np.clip(audio_np, -1.0, 1.0)
            audio_int16 = (audio_int16 * 32767).astype(np.int16)

            # Send AudioStart
            await writer.write(AudioStart(rate=16000, width=2, channels=1, format="s16le"))

            chunk_size = 4096  # Wyoming default is 4096, can adjust as needed
            for start in range(0, len(audio_data), chunk_size):
                end = start + chunk_size
                chunk = audio_data[start:end].tobytes()
                await writer.write(AudioChunk(audio=chunk))

            # Send AudioStop to indicate we're done
            await writer.write(AudioStop())
        else:
            print("[remote] Skipped speaking due to empty text.")
