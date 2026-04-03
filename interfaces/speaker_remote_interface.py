"""
speaker_remote_interface.py — TCP voice interface server (runs on the central server).

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
import json

from piper import PiperVoice, SynthesisConfig

from core.interface_mode import InterfaceMode
from core.session_state import (
    KEY_INTERFACE_MODE, KEY_AGENT_MODE,
    hangup_requested, clear_hangup,
    get_response_queue,
)
from core.speaker_id import SpeakerIdentifier, load_profiles

from whisper_live.vad import VoiceActivityDetector
from faster_whisper import WhisperModel

from core.logger import get_logger
log = get_logger('speaker_remote')


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
    endpoint_id:   Optional[str] = None
    friendly_name: Optional[str] = None

    # ── Core session (resets on CLOS, persists across turns within a session) ─
    session_id: Optional[str] = None

    # ── Session stack (for relay sessions) ───────────────────────────────────
    # When a relay is initiated, the current session is pushed here and
    # restored when the relay completes.
    session_stack: list = field(default_factory=list)

    # ── Audio accumulation ────────────────────────────────────────────────────
    frames_np:     np.ndarray      = field(default_factory=lambda: np.array([], dtype=np.float32))
    recording:     bool            = False
    last_voice_ts: Optional[float] = None

    # ── Flow control ──────────────────────────────────────────────────────────
    rx_paused: bool = True   # starts True; opened after HELO greeting

    # ── Barge-in ──────────────────────────────────────────────────────────────
    interrupt_event: asyncio.Event          = field(default_factory=asyncio.Event)
    _last_int0_ts:   float                  = 0.0
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

class SpeakerRemoteInterface:
    """
    Multi-connection TCP voice server with persistent connections and an
    endpoint registry.

    Public API for server-initiated interactions:
        await sri.initiate_call(endpoint_id, announcement="")
        sri.list_endpoints() -> list[str]
        sri.endpoint_count() -> int
    """

    def __init__(
        self,
        core_processor,
        listening_rate:         int = 16000,
        transcriber             = None,
        vad                     = None,
        piper_voice             = None,
        whisper_model_size:     str = 'base.en',
        piper_max_concurrent:   int = 1,
        whisper_max_concurrent: int = 1,
    ):
        self.core_processor = core_processor
        self.listening_rate = listening_rate

        # ── Endpoint registry ─────────────────────────────────────────────────
        self._endpoints:     Dict[str, ClientSession] = {}
        self._registry_lock: threading.Lock           = threading.Lock()

        # ── Shared config ─────────────────────────────────────────────────────
        self._vad_threshold  = vad.threshold  if vad else 0.5
        self._vad_frame_rate = vad.frame_rate if vad else listening_rate
        self.vad_timeout     = 0.7

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
        if piper_voice is not None:
            log.info("Using shared Piper model")
            self._piper_voice_instance = piper_voice
        else:
            log.info("Loading Piper model")
            self._piper_voice_instance = PiperVoice.load(
                core_processor.config.voice.model_path,
                use_cuda=core_processor.config.voice.use_cuda,
            )
        self._piper_max_concurrent = piper_max_concurrent
        self._piper_pool: Optional[_InferencePool] = None   # created in run()

        # ── Whisper ───────────────────────────────────────────────────────────
        if transcriber is not None:
            log.info("Using shared Whisper model")
            self._whisper_instance = transcriber
        else:
            log.info("Loading Whisper model")
            self._whisper_instance = WhisperModel(model_size_or_path=whisper_model_size)
        self._whisper_max_concurrent = whisper_max_concurrent
        self._whisper_pool: Optional[_InferencePool] = None   # created in run()

        # ── Speaker profiles ──────────────────────────────────────────────────
        config_dir = os.path.join(os.path.dirname(__file__), '../config')
        self._speaker_profiles = load_profiles(config_dir)
        from core.speaker_id import _get_encoder
        _get_encoder()

        log.info("Ready")

    # ── Registry API ─────────────────────────────────────────────────────────

    def _register(self, endpoint_id: str, cs: ClientSession):
        """Add or replace a connected endpoint in the registry."""
        with self._registry_lock:
            self._endpoints[endpoint_id] = cs
        log.info("Endpoint registered", extra={'data': f"{endpoint_id!r} '{cs.friendly_name}' {cs.addr} total={len(self._endpoints)}"})

    def _unregister(self, endpoint_id: str):
        """Remove an endpoint from the registry on disconnect."""
        with self._registry_lock:
            cs = self._endpoints.pop(endpoint_id, None)
        friendly = cs.friendly_name if cs else endpoint_id
        log.info("Endpoint unregistered", extra={'data': f"{endpoint_id!r} '{friendly}' total={len(self._endpoints)}"})

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

        Returns True if the CALL was sent, False if endpoint is not connected
        or is currently busy.
        """
        cs = self.get_endpoint(endpoint_id)
        if cs is None:
            log.warning("initiate_call: endpoint not connected", extra={'data': f"{endpoint_id!r}"})
            return False

        if cs.rx_paused:
            log.warning("initiate_call: endpoint busy", extra={'data': f"{endpoint_id!r}"})
            return False

        try:
            payload = announcement.encode("utf-8") if announcement else b""
            cs.writer.write(pack_frame(b'CALL', payload))
            await cs.writer.drain()
            log.info("CALL sent", extra={'data': f"{endpoint_id!r}"})
            return True
        except Exception as e:
            log.error("initiate_call error", extra={'data': f"{endpoint_id!r}"}, exc_info=True)
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
                log.error("PCM write error", extra={'data': str(cs.addr)}, exc_info=True)
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
                    log.error("Piper TTS error", extra={'data': str(cs.addr)}, exc_info=True)
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
        """Greet the satellite and send RDY0."""
        cs.rx_paused = True
        await self._speak_text(cs, "I'm here")
        cs.writer.write(pack_frame(b'RDY0'))
        await cs.writer.drain()
        cs.rx_paused = False

    async def _open_channel_silent(self, cs: ClientSession, announcement: str):
        """
        Server-initiated call path — no greeting, no mic open.
        Goes straight to the LLM with the announcement as the opening turn.
        """
        cs.rx_paused = True
        await self._contact_core(cs, announcement, silent_start=True)

    async def _close_channel(self, cs: ClientSession):
        """
        End a voice session. Sends CLOS but does NOT close the TCP connection.
        Resets session_id so the next WAKE starts a fresh conversation.
        """
        cs.session_id = None
        cs.rx_paused  = False
        cs.writer.write(pack_frame(b'CLOS'))
        await cs.writer.drain()

    def push_session(self, endpoint_id: str) -> str | None:
        """
        Suspend the current session for an endpoint by pushing it onto the stack.
        Returns the suspended session_id, or None if no session was active.
        Called by contact_user when initiating a relay to this endpoint.
        """
        cs = self.get_endpoint(endpoint_id)
        if cs is None or cs.session_id is None:
            return None
        cs.session_stack.append(cs.session_id)
        suspended = cs.session_id
        cs.session_id = None
        log.info("Session pushed to stack",
                 extra={'data': f"endpoint={endpoint_id} session={suspended}"})
        return suspended

    def pop_session(self, endpoint_id: str, context_note: str = None) -> str | None:
        """
        Resume the most recently suspended session for an endpoint.
        Optionally injects a context note into the resumed session history.
        Returns the resumed session_id, or None if stack was empty.
        Called by reply_to_caller when the relay completes.
        """
        cs = self.get_endpoint(endpoint_id)
        if cs is None or not cs.session_stack:
            return None
        session_id    = cs.session_stack.pop()
        cs.session_id = session_id

        if context_note:
            session = self.core_processor.get_session(session_id)
            if session:
                from core.session_state import get_history
                get_history(session).append({
                    'role':    'system',
                    'content': context_note,
                })

        log.info("Session popped from stack",
                 extra={'data': f"endpoint={endpoint_id} session={session_id}"})
        return session_id

    def set_relay_session(self, endpoint_id: str, session_id: str):
        """
        Set a relay session as the active session for an endpoint.
        Called by contact_user after creating the relay session.
        """
        cs = self.get_endpoint(endpoint_id)
        if cs:
            cs.session_id = session_id
            log.info("Relay session set active",
                     extra={'data': f"endpoint={endpoint_id} session={session_id}"})

    # ── LLM dispatch ─────────────────────────────────────────────────────────

    async def _contact_core(self, cs: ClientSession, input_text: str, silent_start: bool = False) -> bool:
        """Send transcript to LLM, stream TTS back. Returns True if session should close."""
        if cs.session_id is None:
            cs.session_id = str(uuid.uuid4())
            core_session  = self.core_processor.create_session(cs.session_id)
            # Set interface mode and identity
            core_session[KEY_INTERFACE_MODE] = InterfaceMode.SPEAKER
            core_session['endpoint_id']      = cs.endpoint_id
            core_session['interface']        = InterfaceMode.SPEAKER.value
            if cs._identified_speaker:
                core_session['speaker'] = cs._identified_speaker

            if not silent_start:
                await self._speak_text(cs, "Working")
                # Satellite moved to SPEAKING during "Working" — send THNK
                # so it transitions back to THINKING while the LLM generates.
                cs.writer.write(pack_frame(b'THNK'))
                await cs.writer.drain()

        log.info("Sending to core", extra={'data': f"{cs.endpoint_id} {input_text!r}"})
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": cs.session_id},
            daemon=True,
        )
        thread.start()

        core_session = self.core_processor.get_session(cs.session_id)
        buffer       = ""
        cs.rx_paused = True

        while True:
            chunk = await asyncio.to_thread(get_response_queue(core_session).get)
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

        # Check if hangup was requested via tool
        close = hangup_requested(core_session)
        if close:
            clear_hangup(core_session)
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
                f"speaker_{cs.endpoint_id or cs.addr[0]}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
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
            log.error("ASR error", extra={'data': str(cs.addr)}, exc_info=True)
            cs.rx_paused = False
            return

        if not segments:
            cs.rx_paused = False
            return

        text = " ".join(seg.text for seg in segments)
        log.info("Transcription", extra={'data': f"{cs.endpoint_id} {text!r}"})

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
            log.info("Speaker identified", extra={'data': f"{cs.endpoint_id} {cs._identified_speaker}"})
            # Update presence registry with confirmed voice ID
            if hasattr(self.core_processor, 'presence_registry'):
                user_id = self.core_processor.presence_registry.find_user_by_contact(
                    'speaker', 'endpoint_id', cs.endpoint_id
                )
                if not user_id:
                    # Try matching by friendly name from speaker profiles
                    from core.presence_registry import PresenceRegistry
                    for uid in self.core_processor.presence_registry.all_users():
                        name = self.core_processor.presence_registry.get_friendly_name(uid)
                        if name.lower() == cs._identified_speaker.lower():
                            user_id = uid
                            break
                if user_id:
                    self.core_processor.presence_registry.set_last_seen(
                        user_id, cs.endpoint_id, confidence='voice_confirmed'
                    )

        await self._contact_core(cs, text)

    # ── Per-connection handler ────────────────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Coroutine for one persistent satellite connection.
        Handles the full connection lifetime including multiple WAKE/CLOS cycles.
        """
        addr = writer.get_extra_info('peername')
        log.info("Satellite connected", extra={'data': str(addr)})

        cs              = ClientSession(reader=reader, writer=writer, addr=addr)
        cs.vad_detector = VoiceActivityDetector(threshold=self._vad_threshold, frame_rate=self._vad_frame_rate)
        cs._speaker_id  = SpeakerIdentifier(self._speaker_profiles)

        try:
            while True:
                ftype, payload = await read_frame(reader)

                # ── HELO: endpoint registration ───────────────────────────────
                if ftype == b'HELO':
                    try:
                        helo          = json.loads(payload.decode("utf-8", errors="replace"))
                        endpoint_id   = helo.get("id", "unknown")
                        friendly_name = helo.get("name", endpoint_id)
                    except Exception:
                        endpoint_id   = payload.decode("utf-8", errors="replace").strip()
                        friendly_name = endpoint_id
                    cs.endpoint_id   = endpoint_id
                    cs.friendly_name = friendly_name
                    self._register(endpoint_id, cs)
                    cs.rx_paused     = False

                # ── WAKE: start (or restart) a voice session ──────────────────
                elif ftype in (b'WAKE', b'OPEN'):
                    announcement = payload.decode("utf-8", errors="replace").strip() if payload else ""
                    if announcement:
                        await self._open_channel_silent(cs, announcement)
                    else:
                        await self._open_channel(cs)

                # ── AUD0: microphone audio ────────────────────────────────────
                elif ftype == b'AUD0':
                    if cs.rx_paused:
                        log.debug("AUD0 dropping frame — rx_paused", extra={'data': str(cs.endpoint_id)})
                        continue

                    audio_frame = (np.frombuffer(payload, dtype=np.int16)
                                   .astype(np.float32) / 32768.0)

                    vad_result = cs.vad_detector(audio_frame=audio_frame)

                    if vad_result:
                        if not cs.recording:
                            cs.recording = True
                            cs.clear_interrupt()
                            cs._speaker_id.start(
                                get_frames    = lambda: cs.frames_np,
                                is_recording  = lambda: cs.recording,
                            )
                        cs.last_voice_ts = time.monotonic()
                        cs.add_frames(audio_frame)

                    elif cs.recording:
                        silence_s = (time.monotonic() - cs.last_voice_ts) if cs.last_voice_ts else 0
                        if (cs.last_voice_ts is not None and silence_s > self.vad_timeout):
                            await self._transcribe_buffer(cs)
                            cs.recording     = False
                            cs.last_voice_ts = None

                # ── INT0: barge-in ────────────────────────────────────────────
                elif ftype == b'INT0':
                    log.info("Barge-in", extra={'data': str(cs.endpoint_id)})
                    cs._last_int0_ts = time.monotonic()
                    cs.interrupt_event.set()
                    if cs._speak_task and not cs._speak_task.done():
                        cs.rx_paused = False
                    if cs.session_id is not None:
                        try:
                            self.core_processor.cancel_active_response(cs.session_id)
                        except Exception as e:
                            log.error("Cancel error", extra={'data': str(cs.endpoint_id)}, exc_info=True)
                    cs.reset_audio()

                # ── STOP: explicit end-of-utterance ───────────────────────────
                elif ftype == b'STOP':
                    if cs.recording:
                        await self._transcribe_buffer(cs)
                        cs.recording     = False
                        cs.last_voice_ts = None

        except asyncio.IncompleteReadError:
            pass   # normal disconnect

        finally:
            log.info("Satellite disconnected", extra={'data': f"{addr} id={cs.endpoint_id!r}"})
            if cs.endpoint_id:
                self._unregister(cs.endpoint_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def send_relay_message(self, endpoint_id: str, message: str):
        """
        Deliver a relay opening message to an endpoint by initiating a call.
        Called by contact_user generically across interfaces.
        """
        loop = getattr(self, '_loop', None)
        if loop is None:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                log.error("No event loop available for send_relay_message")
                return
        import asyncio
        asyncio.run_coroutine_threadsafe(
            self.initiate_call(endpoint_id, message),
            loop,
        )

    # ── Server entry point ────────────────────────────────────────────────────

    async def run(self, host: str = '0.0.0.0', port: int = 10400):
        """Start the server. Initialises inference pools inside the running loop."""
        self._piper_pool   = _InferencePool(self._piper_voice_instance,  self._piper_max_concurrent)
        self._whisper_pool = _InferencePool(self._whisper_instance, self._whisper_max_concurrent)

        server = await asyncio.start_server(self._handle_client, host, port)
        addr   = ', '.join(str(sock.getsockname()) for sock in server.sockets)
        log.info("Listening", extra={'data': addr})
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    from core.core import CoreProcessor
    from core.settings import AppConfig

    config = AppConfig.load()
    core   = CoreProcessor(config)
    sri    = SpeakerRemoteInterface(core)
    asyncio.run(sri.run())