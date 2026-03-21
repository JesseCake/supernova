"""
voice_remote.py — TCP voice interface server (runs on the central server).

Persistent-connection + endpoint registry model
─────────────────────────────────────────────────
Satellites connect once at startup and stay connected. On connect they send a
HELO frame with their endpoint_id. The server maintains a live registry:

    self.endpoints: dict[endpoint_id → ClientSession]

This gives the server a complete picture of which satellites are online at any
time, and enables server-initiated interactions via initiate_call(endpoint_id).

New frames (added to protocol):

  Client → Server:
    HELO     UTF-8 payload = endpoint_id (first frame after TCP connect)

  Server → Client:
    CALL     UTF-8 payload = optional announcement text, or empty for plain wake
             Sent by initiate_call() to start a session on the satellite.

All other frames are unchanged from the previous version.

Session lifecycle (server side):
  HELO received         → register endpoint, open channel (greet + RDY0)
  WAKE received         → re-open channel if a previous session had closed
  Session (multi-turn)  → exactly as before
  CLOS sent             → session ends, ClientSession stays in registry
  TCP disconnect        → ClientSession removed from registry

The ClientSession dataclass is unchanged. The registry maps endpoint_id to the
live ClientSession so that initiate_call() can reach the right writer.
"""

import asyncio
import struct
import uuid
import re
import time
import threading
import wave
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, Dict

import numpy as np
import resampy
import os

from piper import PiperVoice, SynthesisConfig

from core.precontext import VoiceMode
from core.speaker_id import SpeakerIdentifier, load_profiles

from whisper_live.vad import VoiceActivityDetector
from faster_whisper import WhisperModel


# ── Frame protocol ────────────────────────────────────────────────────────────

FRAME_HDR = struct.Struct('<4sI')

def pack_frame(ftype: bytes, payload: bytes = b'') -> bytes:
    return FRAME_HDR.pack(ftype, len(payload)) + payload

async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)

async def read_frame(reader: asyncio.StreamReader) -> Tuple[bytes, bytes]:
    header        = await read_exactly(reader, FRAME_HDR.size)
    ftype, length = FRAME_HDR.unpack(header)
    payload       = b''
    if length:
        payload = await read_exactly(reader, length)
    return ftype, payload


# ── Resource pool ─────────────────────────────────────────────────────────────

class _InferencePool:
    """
    Semaphore-gated wrapper around a single shared inference model.
    async with pool as model: — blocks until a slot is available.
    max_concurrent=1 serialises all inference (correct for low-traffic use).
    """

    def __init__(self, model, max_concurrent: int = 1):
        self.model = model
        self._sem  = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self):
        await self._sem.acquire()
        return self.model

    async def __aexit__(self, *_):
        self._sem.release()


# ── Per-connection state ──────────────────────────────────────────────────────

@dataclass
class ClientSession:
    """
    All mutable state for one active satellite connection.

    Includes endpoint_id once the HELO frame has been received.
    The session persists for the lifetime of the TCP connection — it is NOT
    torn down between voice sessions (wake→CLOS cycles).
    """

    # ── TCP streams ───────────────────────────────────────────────────────────
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr:   tuple

    # ── Endpoint identity (set on HELO) ───────────────────────────────────────
    # None until the first HELO frame is received. Used as the registry key.
    endpoint_id: Optional[str] = None

    # ── Core session (resets on CLOS, persists across turns within a session) ─
    session_id: Optional[str] = None

    # ── Audio accumulation ────────────────────────────────────────────────────
    frames_np:     np.ndarray      = field(default_factory=lambda: np.array([], dtype=np.float32))
    recording:     bool            = False
    last_voice_ts: Optional[float] = None

    # ── Flow control ──────────────────────────────────────────────────────────
    rx_paused: bool = True   # starts True; opened after HELO greeting

    # ── Barge-in ──────────────────────────────────────────────────────────────
    interrupt_event: asyncio.Event        = field(default_factory=asyncio.Event)
    _last_int0_ts:   float                = 0.0
    _speak_task:     Optional[asyncio.Task] = None

    # ── Speaker identification ────────────────────────────────────────────────
    _speaker_id:         Optional[SpeakerIdentifier] = None
    _identified_speaker: Optional[str]               = None

    # ── VAD (stateful — one per connection) ───────────────────────────────────
    vad_detector: Optional[object] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def reset_audio(self):
        self.frames_np     = np.array([], dtype=np.float32)
        self.recording     = False
        self.last_voice_ts = None

    def add_frames(self, frame_np: np.ndarray):
        if self.frames_np.size == 0:
            self.frames_np = frame_np.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, frame_np))

    def clear_interrupt(self):
        if self.interrupt_event.is_set():
            self.interrupt_event.clear()


# ── Main server class ─────────────────────────────────────────────────────────

class VoiceRemoteInterface:
    """
    Multi-connection TCP voice server with persistent connections and an
    endpoint registry.

    Public API for server-initiated interactions:
        await vr.initiate_call(endpoint_id, announcement="")
        vr.list_endpoints() -> list[str]
        vr.endpoint_count() -> int
    """

    def __init__(
        self,
        core_processor,
        listening_rate:          int = 16000,
        whisper_model_size:      str = 'base.en',
        piper_max_concurrent:    int = 1,
        whisper_max_concurrent:  int = 1,
    ):
        self.core_processor = core_processor
        self.listening_rate = listening_rate

        # ── Endpoint registry ─────────────────────────────────────────────────
        # Maps endpoint_id → ClientSession for all currently connected satellites.
        # Protected by _registry_lock for thread-safe reads from non-async code.
        self._endpoints: Dict[str, ClientSession] = {}
        self._registry_lock = threading.Lock()

        # ── Shared config ─────────────────────────────────────────────────────
        self.vad_timeout          = 0.7
        self.close_channel_phrase = "finish conversation"
        self.speaking_rate        = 16000

        self.sentence_endings = re.compile(
            r'(?:(?<=[!?])(?:\s+|$))'
            r'|(?:(?<=\.)(?!\d)(?:\s+|$))'
            r'|(?:(?<=[,;])(?:\s+|$))'
            r'|[\r\n]+'
        )
        self.piper_syn_config = SynthesisConfig(
            volume=1.0, length_scale=1.0,
            noise_scale=1.0, noise_w_scale=1.0,
            normalize_audio=False,
        )

        # ── Piper ─────────────────────────────────────────────────────────────
        print("[voice_remote] Loading Piper TTS model...")
        self._piper_voice_instance = PiperVoice.load(
            core_processor.config.voice.model_path,
            use_cuda=core_processor.config.voice.use_cuda,
        )
        self._piper_max_concurrent = piper_max_concurrent
        self._piper_pool: Optional[_InferencePool] = None   # created in run()

        # ── Whisper ───────────────────────────────────────────────────────────
        print("[voice_remote] Loading Whisper model...")
        self._whisper_instance       = WhisperModel(model_size_or_path=whisper_model_size)
        self._whisper_max_concurrent = whisper_max_concurrent
        self._whisper_pool: Optional[_InferencePool] = None   # created in run()

        # ── Speaker profiles ──────────────────────────────────────────────────
        config_dir = os.path.join(os.path.dirname(__file__), '../config')
        self._speaker_profiles = load_profiles(config_dir)
        from core.speaker_id import _get_encoder
        _get_encoder()

        print("[voice_remote] Ready.")

    # ── Registry API ─────────────────────────────────────────────────────────

    def _register(self, endpoint_id: str, cs: ClientSession):
        """Add or replace a connected endpoint in the registry."""
        with self._registry_lock:
            self._endpoints[endpoint_id] = cs
        print(f"[registry] registered: {endpoint_id!r} ({cs.addr}) — "
              f"{len(self._endpoints)} endpoint(s) online")

    def _unregister(self, endpoint_id: str):
        """Remove an endpoint from the registry on disconnect."""
        with self._registry_lock:
            self._endpoints.pop(endpoint_id, None)
        print(f"[registry] unregistered: {endpoint_id!r} — "
              f"{len(self._endpoints)} endpoint(s) online")

    def list_endpoints(self) -> list:
        """Return a list of currently connected endpoint IDs."""
        with self._registry_lock:
            return list(self._endpoints.keys())

    def endpoint_count(self) -> int:
        """Return the number of currently connected endpoints."""
        with self._registry_lock:
            return len(self._endpoints)

    def get_endpoint(self, endpoint_id: str) -> Optional[ClientSession]:
        """Return the ClientSession for endpoint_id, or None if not connected."""
        with self._registry_lock:
            return self._endpoints.get(endpoint_id)

    # ── Server-initiated call ─────────────────────────────────────────────────

    async def initiate_call(self, endpoint_id: str, announcement: str = "") -> bool:
        """
        Push a CALL frame to a connected satellite to start a server-initiated
        session — without the user needing to say the wake word.

        Use cases:
          - Announcements: "Dinner is ready"
          - Alerts: "Your timer has gone off"
          - Proactive interactions: "You have a meeting in 10 minutes"
          - Any server-side event that should speak to the user

        The satellite treats CALL the same as a locally detected wake word: it
        sends WAKE back, and the normal session flow begins. If announcement is
        non-empty the satellite can optionally display or pre-populate it
        (currently just logged; future work could inject it as the opening line).

        Returns True if the CALL was sent, False if the endpoint is not connected
        or is currently in an active session (rx_paused = True means it's busy).
        """
        cs = self.get_endpoint(endpoint_id)
        if cs is None:
            print(f"[registry] initiate_call: {endpoint_id!r} not connected")
            return False

        if cs.rx_paused:
            print(f"[registry] initiate_call: {endpoint_id!r} is busy (rx_paused)")
            return False

        try:
            payload = announcement.encode("utf-8") if announcement else b""
            cs.writer.write(pack_frame(b'CALL', payload))
            await cs.writer.drain()
            print(f"[registry] CALL sent to {endpoint_id!r}")
            return True
        except Exception as e:
            print(f"[registry] initiate_call error for {endpoint_id!r}: {e}")
            return False

    # ── Low-level send helpers ────────────────────────────────────────────────

    async def _send_pcm_int16(self, cs: ClientSession, tag: bytes,
                               audio_int16: np.ndarray, chunk_samples: int = 1024):
        mv   = memoryview(audio_int16.tobytes())
        step = chunk_samples * 2
        for i in range(0, len(mv), step):
            if cs.interrupt_event.is_set():
                break
            try:
                cs.writer.write(pack_frame(tag, mv[i:i + step].tobytes()))
            except Exception as e:
                print(f"[voice_remote:{cs.addr}] pcm write error: {e}")
                break
            await cs.writer.drain()
            await asyncio.sleep(0)

    # ── TTS ───────────────────────────────────────────────────────────────────

    async def _speak_text(self, cs: ClientSession, text: str):
        """
        Synthesise text with Piper (pool-gated) and stream as TTS0 frames.
        Half-duplex: holds cs.rx_paused for the duration.
        """
        prev_rx_paused = cs.rx_paused
        cs.rx_paused   = True
        try:
            text = re.sub(r'[*#`_~]', '', text).strip()
            if not text:
                return

            async with self._piper_pool as voice:
                syn_config = self.piper_syn_config

                def synth_all_chunks():
                    chunks = []
                    for chunk in voice.synthesize(text, syn_config=syn_config):
                        chunks.append((chunk.sample_rate, chunk.audio_float_array.copy()))
                    return chunks

                try:
                    piper_chunks = await asyncio.to_thread(synth_all_chunks)
                except Exception as e:
                    print(f"[voice_remote:{cs.addr}] Piper error: {e}")
                    return

            for sr, audio_f32 in piper_chunks:
                if cs.interrupt_event.is_set():
                    return
                audio_f32 = np.asarray(audio_f32, dtype=np.float32).reshape(-1)
                if sr != self.speaking_rate:
                    audio_f32 = resampy.resample(audio_f32, sr, self.speaking_rate)
                rms = float(np.sqrt(np.mean(audio_f32 ** 2))) if audio_f32.size else 0.0
                if rms > 1e-8:
                    audio_f32 *= (0.2 / rms)
                audio_f32   = np.clip(audio_f32 * 1.2, -1.0, 1.0)
                audio_int16 = (audio_f32 * 32767.0).astype(np.int16)
                await self._send_pcm_int16(cs, b"TTS0", audio_int16, chunk_samples=8192)
                if cs.interrupt_event.is_set():
                    return
        finally:
            cs.rx_paused = prev_rx_paused

    # ── Channel lifecycle ─────────────────────────────────────────────────────

    async def _open_channel(self, cs: ClientSession):
        """
        Greet the satellite and send RDY0.

        Called both on HELO (first connect) and on WAKE (subsequent sessions
        on the same persistent connection after a previous CLOS).
        """
        cs.rx_paused = True
        await self._speak_text(cs, "I'm here")
        cs.writer.write(pack_frame(b'RDY0'))
        await cs.writer.drain()
        cs.rx_paused = False

    async def _close_channel(self, cs: ClientSession):
        """
        End a voice session. Sends CLOS but does NOT close the TCP connection.
        Resets session_id so the next WAKE starts a fresh conversation.
        """
        cs.session_id = None
        cs.rx_paused  = False
        cs.writer.write(pack_frame(b'CLOS'))
        await cs.writer.drain()

    # ── LLM dispatch ─────────────────────────────────────────────────────────

    async def _contact_core(self, cs: ClientSession, input_text: str) -> bool:
        """Send transcript to LLM, stream TTS back. Returns True if session should close."""
        if cs.session_id is None:
            cs.session_id = str(uuid.uuid4())
            self.core_processor.create_session(cs.session_id)
            core_session = self.core_processor.get_session(cs.session_id)
            if core_session is not None and cs._identified_speaker:
                core_session['speaker'] = cs._identified_speaker
            await self._speak_text(cs, "Working")

        print(f"[voice_remote:{cs.endpoint_id}] → core: {input_text!r}")
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": cs.session_id, "mode": VoiceMode.SPEAKER},
            daemon=True,
        )
        thread.start()

        core_session = self.core_processor.get_session(cs.session_id)
        buffer       = ""
        cs.rx_paused = True

        while True:
            chunk = await asyncio.to_thread(core_session['response_queue'].get)
            if chunk is None:
                break
            buffer += chunk
            sentences = self.sentence_endings.split(buffer)
            for sent in sentences[:-1]:
                sent = sent.strip().replace("*", "")
                if sent:
                    if cs.interrupt_event.is_set():
                        buffer = ""
                        break
                    await self._speak_text(cs, sent)
            buffer = sentences[-1]

        if cs.interrupt_event.is_set():
            buffer = ""
        if buffer.strip():
            await self._speak_text(cs, buffer.strip())

        close = core_session['close_voice_channel'].is_set()
        if close:
            core_session['close_voice_channel'].clear()
            await self._close_channel(cs)
        else:
            cs.rx_paused = False
            cs.writer.write(pack_frame(b'RDY0'))
            await cs.writer.drain()

        return close

    # ── Transcription ─────────────────────────────────────────────────────────

    async def _transcribe_buffer(self, cs: ClientSession):
        """Transcribe audio, send THNK, collect speaker ID, dispatch to core."""
        cs.rx_paused = True

        if cs.frames_np.size == 0:
            cs.rx_paused = False
            return

        min_samples = int(0.2 * self.listening_rate)
        if cs.frames_np.size < min_samples:
            cs.reset_audio()
            cs.rx_paused = False
            return

        audio_snapshot = cs.frames_np.copy()
        cs.reset_audio()

        if self.core_processor.config.debug.record_audio:
            record_dir = self.core_processor.config.debug.record_dir
            os.makedirs(record_dir, exist_ok=True)
            filename = os.path.join(
                record_dir,
                f"remote_{cs.endpoint_id or cs.addr[0]}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
            )
            with wave.open(filename, 'wb') as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(self.listening_rate)
                wf.writeframes((audio_snapshot * 32767).astype(np.int16).tobytes())

        try:
            async with self._whisper_pool as whisper:
                audio_for_thread = audio_snapshot
                def do_transcribe():
                    return whisper.transcribe(audio_for_thread)
                segments, _ = await asyncio.to_thread(do_transcribe)
        except Exception as e:
            print(f"[voice_remote:{cs.addr}] ASR error: {e}")
            cs.rx_paused = False
            return

        if not segments:
            cs.rx_paused = False
            return

        text = " ".join(seg.text for seg in segments)
        print(f"[voice_remote:{cs.endpoint_id}] transcription: {text!r}")

        if self.close_channel_phrase in text.lower():
            await self._close_channel(cs)
            return
        if re.fullmatch(r'[\s.…]+', text):
            cs.rx_paused = False
            return

        cs.writer.write(pack_frame(b'THNK'))
        await cs.writer.drain()

        if cs._speak_task and not cs._speak_task.done():
            await cs._speak_task

        cs._identified_speaker = cs._speaker_id.result(timeout=1.0)
        if cs._identified_speaker:
            print(f"[voice_remote:{cs.endpoint_id}] speaker: {cs._identified_speaker}")

        await self._contact_core(cs, text)

    # ── Per-connection handler ────────────────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Coroutine for one persistent satellite connection.

        Handles the full connection lifetime:
          - Waits for HELO to get endpoint_id and register in the registry.
          - Dispatches all subsequent frames (AUD0, WAKE, INT0, STOP).
          - Multiple WAKE/CLOS cycles are handled on the same connection.
          - Unregisters on disconnect.
        """
        addr = writer.get_extra_info('peername')
        print(f"[voice_remote] satellite connected: {addr}")

        cs             = ClientSession(reader=reader, writer=writer, addr=addr)
        cs.vad_detector = VoiceActivityDetector(threshold=0.5, frame_rate=self.listening_rate)
        cs._speaker_id  = SpeakerIdentifier(self._speaker_profiles)

        try:
            while True:
                ftype, payload = await read_frame(reader)

                # ── HELO: endpoint registration ───────────────────────────────
                if ftype == b'HELO':
                    endpoint_id     = payload.decode("utf-8", errors="replace").strip()
                    cs.endpoint_id  = endpoint_id
                    self._register(endpoint_id, cs)
                    cs.rx_paused   = False

                # ── WAKE: start (or restart) a voice session ──────────────────
                elif ftype in (b'WAKE', b'OPEN'):
                    # On a persistent connection, WAKE can arrive at the start
                    # of each new wake-word session. Re-open the channel.
                    await self._open_channel(cs)

                # ── AUD0: microphone audio ────────────────────────────────────
                elif ftype == b'AUD0':
                    if cs.rx_paused:
                        print(f"[aud0] dropping frame — rx_paused")
                        continue

                    audio_frame = (np.frombuffer(payload, dtype=np.int16)
                                   .astype(np.float32) / 32768.0)

                    vad_result = cs.vad_detector(audio_frame=audio_frame)
                    
                    # Debugging for receiving audio frames:
                    #print(f"[aud0] vad={vad_result} recording={cs.recording} frames={cs.frames_np.size}")

                    if vad_result:
                        if not cs.recording:
                            # Debug checking VAD rising edge
                            #print(f"[aud0] VAD rising edge — start recording")
                            cs.recording = True
                            cs.clear_interrupt()
                            cs._speaker_id.start(
                                get_frames=lambda: cs.frames_np,
                                is_recording=lambda: cs.recording,
                            )
                        cs.last_voice_ts = time.monotonic()
                        cs.add_frames(audio_frame)

                    elif cs.recording:
                        silence_s = (time.monotonic() - cs.last_voice_ts) if cs.last_voice_ts else 0
                        
                        # Debugging for silence timing before firing:
                        #print(f"[aud0] silence={silence_s:.2f}s (threshold={self.vad_timeout}s)")
                        
                        if (cs.last_voice_ts is not None and silence_s > self.vad_timeout):
                            # Debug vad ending trigger (falling edge)
                            #print(f"[aud0] VAD timeout — triggering transcription")
                            await self._transcribe_buffer(cs)
                            cs.recording     = False
                            cs.last_voice_ts = None

                # ── INT0: barge-in ────────────────────────────────────────────
                elif ftype == b'INT0':
                    print(f"[voice_remote:{cs.endpoint_id}] barge-in")
                    cs._last_int0_ts = time.monotonic()
                    cs.interrupt_event.set()
                    if cs._speak_task and not cs._speak_task.done():
                        cs.rx_paused = False
                    if cs.session_id is not None:
                        try:
                            self.core_processor.cancel_active_response(cs.session_id)
                        except Exception as e:
                            print(f"[voice_remote:{cs.endpoint_id}] cancel error: {e}")
                    cs.reset_audio()

                # ── STOP: explicit end-of-utterance ───────────────────────────
                elif ftype == b'STOP':
                    if cs.recording:
                        await self._transcribe_buffer(cs)
                        cs.recording     = False
                        cs.last_voice_ts = None

                # Unknown tags silently ignored.

        except asyncio.IncompleteReadError:
            pass   # normal disconnect

        finally:
            print(f"[voice_remote] satellite disconnected: {addr} (id={cs.endpoint_id!r})")
            if cs.endpoint_id:
                self._unregister(cs.endpoint_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Server entry point ────────────────────────────────────────────────────

    async def run(self, host: str = '0.0.0.0', port: int = 10400):
        """Start the server. Initialises inference pools inside the running loop."""
        self._piper_pool   = _InferencePool(self._piper_voice_instance,   self._piper_max_concurrent)
        self._whisper_pool = _InferencePool(self._whisper_instance, self._whisper_max_concurrent)

        server = await asyncio.start_server(self._handle_client, host, port)
        addr   = ', '.join(str(sock.getsockname()) for sock in server.sockets)
        print(f"[voice_remote] Listening on {addr} (persistent connections, endpoint registry)")
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    from core.core import CoreProcessor
    from core.settings import AppConfig

    config = AppConfig.load()
    core   = CoreProcessor(config)
    vr     = VoiceRemoteInterface(core)
    asyncio.run(vr.run())