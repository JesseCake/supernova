
import asyncio
import struct
import uuid
import re
import time
import threading
from typing import Optional, Tuple

import numpy as np
import resampy
import torch
from TTS.api import TTS as CoquiTTS

import TTS.tts
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel

FRAME_HDR = struct.Struct('<4sI')  # (type:4s, length:uint32)

def pack_frame(ftype: bytes, payload: bytes = b'') -> bytes:
    return FRAME_HDR.pack(ftype, len(payload)) + payload

async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)

async def read_frame(reader: asyncio.StreamReader) -> Tuple[bytes, bytes]:
    header = await read_exactly(reader, FRAME_HDR.size)
    ftype, length = FRAME_HDR.unpack(header)
    payload = b''
    if length:
        payload = await read_exactly(reader, length)
    return ftype, payload

class VoiceRemoteInterface:
    """
    Voice interface using a tiny raw-PCM TCP protocol.
    One satellite at a time.

    Client frames -> server:
      - b"WAKE" or b"OPEN": no payload. Ask server to acknowledge.
      - b"AUD0": int16 mono 16k PCM chunk.
      - b"STOP": no payload. End-of-utterance / flush.

    Server frames -> client:
      - b"TTS0": int16 mono 16k PCM (response speech).
      - b"BEEP": int16 mono 16k PCM (UX cue).
      - b"RDY0": no payload. Server is ready for AUD0 after wake.
      - b"CLOS": no payload. Server is closing channel.
    """

    def __init__(self, core_processor, listening_rate: int = 16000):
        self.core_processor = core_processor
        self.session_id: Optional[str] = None

        # Audio IO config
        self.listening_rate = listening_rate
        self.speaking_rate = 16000
        self.tts_sample_rate = 22050

        # ASR / VAD
        self.vad_detector = VoiceActivityDetector(threshold=0.7, frame_rate=self.listening_rate)
        self.transcriber = WhisperModel(model_size_or_path='tiny.en')
        self.frames_np = np.array([], dtype=np.float32)
        self.recording = False
        self.close_channel_phrase = "finish conversation"

        # TTS
        device = "cuda"
        self.tts = CoquiTTS(model_name="tts_models/en/vctk/vits", progress_bar=False).to(device)
        self._tts_device = device
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'
        self.sentence_endings = re.compile(r'(?<=[.!?])\s+')

        # Connection state
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    # ------------------ protocol helpers ------------------
    async def send_text(self, tag: bytes, text: str):
        self.writer.write(pack_frame(tag, text.encode('utf-8')))
        await self.writer.drain()

    async def send_pcm_int16(self, tag: bytes, audio_int16: np.ndarray, chunk_samples: int = 1024):
        mv = memoryview(audio_int16.tobytes())
        # 2 bytes per sample
        step = chunk_samples * 2
        for i in range(0, len(mv), step):
            self.writer.write(pack_frame(tag, mv[i:i + step].tobytes()))
            await self.writer.drain()

    async def send_beep(self, freq=800, duration=0.15, volume=0.2):
        sr = self.speaking_rate
        t = np.linspace(0, duration, int(sr * duration), False)
        tone = np.sin(2 * np.pi * freq * t).astype(np.float32)
        tone = np.clip(tone * volume, -1.0, 1.0)
        audio_int16 = (tone * 32767).astype(np.int16)
        await self.send_pcm_int16(b'BEEP', audio_int16)

    # ------------------ core plumbing ------------------
    def _add_frames(self, frame_np: np.ndarray):
        if self.frames_np.size == 0:
            self.frames_np = frame_np.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0)

    async def _contact_core(self, input_text: str) -> bool:
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)

        # Kick processing in a thread so we can stream out TTS
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": self.session_id, "is_voice": True},
            daemon=True,
        )
        thread.start()

        session = self.core_processor.get_session(self.session_id)
        buffer = ""
        while True:
            if not session['response_queue'].empty():
                chunk = session['response_queue'].get()
                if chunk is None:
                    break
                buffer += chunk
                sentences = self.sentence_endings.split(buffer)
                for sent in sentences[:-1]:
                    if sent.strip():
                        await self._speak_text(sent.strip())
                buffer = sentences[-1]
            else:
                await asyncio.sleep(0.01)

        if buffer.strip():
            await self._speak_text(buffer.strip())

        return session['close_voice_channel'].is_set()

    async def _transcribe_buffer(self):
        if self.frames_np.size == 0:
            return
        try:
            segments, _ = self.transcriber.transcribe(self.frames_np)
            self.frames_np = np.array([], dtype=np.float32)
        except Exception as e:
            print(f"[voice_remote] ASR error: {e}")
            self.frames_np = np.array([], dtype=np.float32)
            return

        if not segments:
            return

        text = " ".join([seg.text for seg in segments])
        print(f"[voice_remote] Transcription: {text}")
        if self.close_channel_phrase in text.lower():
            await self._close_channel()
            return

        close = await self._contact_core(text)
        if close:
            await self._close_channel()
        else:
            #await self.send_beep(800, 0.10, 0.2)
            pass

    async def _speak_text(self, text: str):
        if not text.strip():
            return
        tts_f32 = np.array(self.tts.tts(text, speed=self.speech_speed, speaker=self.speech_speaker), dtype=np.float32)
        resamp = resampy.resample(tts_f32, self.tts_sample_rate, self.speaking_rate)
        rms = np.sqrt(np.mean(resamp ** 2))
        target_rms = 0.2
        if rms > 0:
            resamp *= target_rms / rms
        resamp = np.clip(resamp * 1.2, -1.0, 1.0)
        audio_int16 = (np.clip(resamp, -1.0, 1.0) * 32767).astype(np.int16)
        await self.send_pcm_int16(b'TTS0', audio_int16)

    async def _open_channel(self):
        # Wake acknowledgement then "ready"
        await self._speak_text("I'm here")
        #await self.send_beep(300, 0.20, 0.2)
        # Tell client to start streaming speech now
        self.writer.write(pack_frame(b'RDY0'))
        await self.writer.drain()

    async def _close_channel(self):
        self.session_id = None
        for _ in range(3):
            await self.send_beep(300, 0.20, 0.2)
            await asyncio.sleep(0.15)
        self.writer.write(pack_frame(b'CLOS'))
        await self.writer.drain()

    # ------------------ server loop ------------------
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        addr = writer.get_extra_info('peername')
        print(f"[voice_remote] satellite connected: {addr}")
        try:
            while True:
                ftype, payload = await read_frame(reader)
                if ftype in (b'OPEN', b'WAKE'):
                    await self._open_channel()
                elif ftype == b'AUD0':
                    audio_frame = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
                    if self.vad_detector(audio_frame=audio_frame):
                        if not self.recording:
                            self.recording = True
                        self._add_frames(audio_frame)
                    elif self.recording:
                        await self._transcribe_buffer()
                        self.recording = False
                elif ftype == b'STOP':
                    if self.recording:
                        await self._transcribe_buffer()
                        self.recording = False
                else:
                    # ignore unknown tags for forward-compat
                    pass
        except asyncio.IncompleteReadError:
            pass
        finally:
            print(f"[voice_remote] satellite disconnected: {addr}")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.reader = None
            self.writer = None
            self.session_id = None
            self.frames_np = np.array([], dtype=np.float32)
            self.recording = False

    async def run(self, host: str = '0.0.0.0', port: int = 10400):
        server = await asyncio.start_server(self._handle_client, host, port)
        addr = ', '.join(str(sock.getsockname()) for sock in server.sockets)
        print(f"[voice_remote] Listening on {addr} (raw PCM)")
        async with server:
            await server.serve_forever()

if __name__ == '__main__':
    import asyncio
    from core.core import CoreProcessor

    core = CoreProcessor()
    vr = VoiceRemoteInterface(core)
    asyncio.run(vr.run())