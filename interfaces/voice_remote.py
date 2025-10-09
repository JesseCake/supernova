
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
      - b"INT0": no payload. Interrupt (cancel) any current TTS and accept new AUD0 immediately.
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
        self.vad_detector = VoiceActivityDetector(threshold=0.5, frame_rate=self.listening_rate)
        self.transcriber = WhisperModel(model_size_or_path='base.en')  # originally we started with tiny.en
        self.frames_np = np.array([], dtype=np.float32)
        self.recording = False
        self.close_channel_phrase = "finish conversation"
        self.last_voice_ts = None  # Timestamp of last received voice activity (so we can tune how long to wait before taking action)
        self.vad_timeout = 0.7  # Seconds of silence to wait before considering utterance complete

        # TTS
        device = "cuda"
        self.tts = CoquiTTS(model_name="tts_models/en/vctk/vits", progress_bar=False).to(device)
        self._tts_device = device
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'
        # Split after ., !, or ? followed by whitespace (or newline / EoS),
        # but only if NOT immediately followed by a digit (negative lookahead)
        self.sentence_endings = re.compile(
            r'(?:(?<=[!?])(?:\s+|$))'        # ! or ? + (space or EoS)
            r'|(?:(?<=\.)(?!\d)(?:\s+|$))'   # . not followed by digit + (space or EoS)
            r'|[\r\n]+'                      # newline boundaries
        )

        # Connection state
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        # interruption event to interrupt TTS playback if needed
        self.interrupt_event = asyncio.Event()   # when set: abort any ongoing TTS send
        self._last_int0_ts = 0.0                 # debug/telemetry only
        self._speak_task: Optional[asyncio.Task] = None

        # Half-duplex RX gate: when True we ignore AUD0/VAD
        self.rx_paused = False 

    # ------------------ protocol helpers ------------------
    async def send_text(self, tag: bytes, text: str):
        self.writer.write(pack_frame(tag, text.encode('utf-8')))
        await self.writer.drain()

    async def send_pcm_int16(self, tag: bytes, audio_int16: np.ndarray, chunk_samples: int = 8192):
        mv = memoryview(audio_int16.tobytes())
        # 2 bytes per sample
        step = chunk_samples * 2
        for i in range(0, len(mv), step):

            # >>> NEW: honor barge-in
            if self.interrupt_event.is_set():
                break
            
            try:
                self.writer.write(pack_frame(tag, mv[i:i + step].tobytes()))
            except Exception as e:
                print(f"[voice_remote] send_pcm_int16 write error: {e}")
                break
        
            await self.writer.drain()
            await asyncio.sleep(0)  # cooperative yield to allow interrupt

    async def send_beep(self, freq=800, duration=0.15, volume=0.6):
        sr = self.speaking_rate
        t = np.linspace(0, duration, int(sr * duration), False)
        tone = np.sin(2 * np.pi * freq * t).astype(np.float32)
        tone = np.clip(tone * volume, -1.0, 1.0)
        audio_int16 = (tone * 32767).astype(np.int16)
        await self.send_pcm_int16(b'BEEP', audio_int16)

    def _interrupt_now(self):
        """Raise the barge-in flag; any send loops will stop at next chunk."""
        self._last_int0_ts = time.monotonic()
        self.interrupt_event.set()

    def _clear_interrupt(self):
        """Lower the barge-in flag so future TTS can proceed."""
        if self.interrupt_event.is_set():
            self.interrupt_event.clear()

    # ------------------ core plumbing ------------------
    def _add_frames(self, frame_np: np.ndarray):
        if self.frames_np.size == 0:
            self.frames_np = frame_np.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0)

    async def _contact_core(self, input_text: str) -> bool:
        #print(f"[debug] start new session..")
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)

            # send immediate response so we know we're live
            #print(f"[debug] sending 'Working' TTS")
            await self._speak_text("Working")

        # Kick processing in a thread so we can stream out TTS
        print(f"[debug] starting thread to process input: {input_text}")
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": self.session_id, "is_voice": True},
            daemon=True,
        )
        thread.start()
        #print(f"[debug] thread started, streaming response..")

        session = self.core_processor.get_session(self.session_id)
        buffer = ""

        # keep the server side half duplex RX gate closed until we finish TTS
        self.rx_paused = True

        while True:
            # block in a thread so the event loop sleeps until a chunk arrives
            chunk = await asyncio.to_thread(session['response_queue'].get)
            if chunk is None:
                break
            #else:
            #    print(f"[voice_debug] got chunk: {chunk}")
            buffer += chunk
            sentences = self.sentence_endings.split(buffer)
            for sent in sentences[:-1]:
                sent = sent.strip().replace("*", "")  # remove * which would be read out loud
                if sent.strip():
                    #print(f"[voice_debug] speaking sentence: {sent.strip()}")
                    #Barge in check:
                    if self.interrupt_event.is_set():
                        # drop buffered text; exit early so we pivot to the user's barge-in
                        buffer = ""
                        break
                    await self._speak_text(sent.strip())
            buffer = sentences[-1]

        if self.interrupt_event.is_set():
            # drop buffered text; exit early so we pivot to the user's barge-in
            buffer = ""

        if buffer.strip():
            #print(f"[voice_debug] speaking final buffer: {buffer.strip()}")
            await self._speak_text(buffer.strip())

        # speaking is finished:
        close = session['close_voice_channel'].is_set()

        if close:
            # Reset the flag for the next turn/session
            session['close_voice_channel'].clear()
            # Strict single-turn close
            await self._close_channel()
        else:
            # Multi-turn: re-arm capture
            self.rx_paused = False
            self.writer.write(pack_frame(b'RDY0'))
            await self.writer.drain()

        return close

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

        
        if self._speak_task and not self._speak_task.done():
            await self._speak_task

        close = await self._contact_core(text)

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
        print(f"[voice_remote] satellite opened channel")
        

        await self._speak_text("I'm here")
        #await self.send_beep(300, 0.20, 0.2)
        # Tell client to start streaming speech now
        await self.writer.drain()

        # Wake acknowledgement then "ready"
        self.writer.write(pack_frame(b'RDY0'))
        await self.writer.drain()

        self.rx_paused = False  # open the RX gate
        
    async def _close_channel(self):
        self.session_id = None
        for _ in range(3):
            await self.send_beep(300, 0.20, 0.6)
            await asyncio.sleep(0.15)
        self.writer.write(pack_frame(b'CLOS'))
        await self.writer.drain()

    # ------------------ server loop ------------------
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        addr = writer.get_extra_info('peername')
        print(f"[voice_remote] satellite connected: {addr}")

        #reset state
        self.recording = False
        self.last_voice_ts = None

        try:
            while True:
                ftype, payload = await read_frame(reader)
                if ftype in (b'OPEN', b'WAKE'):
                    await self._open_channel()

                elif ftype == b'AUD0':
                    #print(f"[voice_remote] received AUD0 ({len(payload)} bytes)")
                    if self.rx_paused:
                        #print(f"[voice_remote] RX paused, ignoring AUD0")
                        continue  # ignore incoming audio while we're speaking or processing

                    audio_frame = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
                    if self.vad_detector(audio_frame=audio_frame):
                        # Detected voice activity:
                        if not self.recording:
                            #print(f"[voice_remote] VAD detected speech, start recording")
                            self.recording = True
                            # NEW: first speech after interrup -> allow future TTS again
                            self._clear_interrupt()
                        self.last_voice_ts = time.monotonic()  # mark latest speech
                        self._add_frames(audio_frame)
                    elif self.recording:
                        # if we've had the threshold of silence, consider utterance complete:
                        if self.last_voice_ts is not None and (time.monotonic() - self.last_voice_ts) > self.vad_timeout:
                            #print(f"[voice_remote] VAD timeout, end of utterance: transcribing..")
                            await self._transcribe_buffer()
                            self.recording = False
                            self.last_voice_ts = None

                elif ftype == b'INT0':
                    # Client requests immediate stop of any TTS (barge-in).
                    print(f"[voice_remote] received INT0 (barge-in)")
                    self._interrupt_now()

                    # optional: cancel the task wrapper (the code inside checks interrupt_event anyway)
                    if self._speak_task and not self._speak_task.done():
                        # don't hard cancel mid-CPU; just let it hit its interrupt checks
                        pass

                    # tell the Core to stop generating and stop enqueueing
                    if self.session_id is not None:
                        try:
                            self.core_processor.cancel_active_response(self.session_id)  # ← NEW
                        except Exception as e:
                            print(f"[voice_remote] cancel error: {e}")

                    # Reset partial-ASR capture so the next speech is a clean utterance.
                    self.frames_np = np.array([], dtype=np.float32)
                    self.recording = False
                    self.last_voice_ts = None

                    # re-arm listening:
                    #self.rx_paused = False
                    #self.writer.write(pack_frame(b'RDY0'))  # ready for more audio

                    # Optional UX cue:
                    # await self.send_beep(1000, 0.06, 0.3)

                elif ftype == b'STOP':
                    if self.recording:
                        await self._transcribe_buffer()
                        self.recording = False
                        self.last_voice_ts = None

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