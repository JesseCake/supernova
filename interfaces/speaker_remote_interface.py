"""
speaker_remote_interface.py — TCP voice interface server (runs on the central server).

Persistent-connection + endpoint registry model
─────────────────────────────────────────────────
Satellites connect once at startup and stay connected. On connect they send a
HELO frame with their endpoint_id. The server maintains a live registry:

    self._endpoints: dict[endpoint_id → VoiceContext]

This gives the server a complete picture of which satellites are online at any
time, and enables server-initiated interactions via initiate_call(endpoint_id).

Frame protocol (client → server):
    HELO   JSON payload: {"id": endpoint_id, "name": friendly_name}
    WAKE   Optional UTF-8 payload = announcement (server-initiated replay path)
    OPEN   Alias for WAKE
    AUD0   Raw int16 PCM audio at 16kHz
    INT0   Barge-in — interrupt current TTS and cancel LLM response
    STOP   Explicit end-of-utterance — force immediate transcription

Frame protocol (server → client):
    CALL   Optional UTF-8 payload = announcement text
           Sent by initiate_call() to wake a satellite without a wake word.
    TTS0   Raw int16 PCM audio at 16kHz (synthesised speech)
    THNK   Satellite should display "thinking" state
    RDY0   Satellite should open microphone / display "listening" state
    CLOS   Session ended — satellite resets, TCP connection stays open

Session lifecycle (server side):
    HELO received         → register endpoint, rx_paused = False (ready for WAKE)
    WAKE received         → greet ("I'm here") + RDY0         [user-initiated]
    WAKE with payload     → silent_start, straight to LLM     [server-initiated relay]
    CALL sent             → satellite initiates WAKE itself
    AUD0 → VAD → silence  → THNK + transcribe + _contact_core → RDY0
    hangup tool           → CLOS (TCP persists)
    TCP disconnect        → unregister endpoint

Relay session model:
    push_session()        → suspend current session onto ctx.session_stack
    set_relay_session()   → activate relay session on ctx
    pop_session()         → restore suspended session, optionally inject context note
"""

import asyncio
import json
import re
import struct
import threading
import time
from typing import Dict, Optional, Tuple

import numpy as np

from core.interface_mode import InterfaceMode
from core.logger import get_logger
from core.session_state import get_history

from interfaces.base_voice_interface import (
    BaseVoiceInterface,
    VoiceContext,
    INTERNAL_RATE,
)

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


# ── Context extension ─────────────────────────────────────────────────────────
# VoiceContext already carries session_stack, friendly_name, speak_task,
# last_int0_ts from the base. We store the TCP writer on a plain attribute
# set after make_context() — avoiding a dataclass dependency on asyncio streams.


class SpeakerRemoteInterface(BaseVoiceInterface):
    """
    Multi-connection TCP voice server with persistent connections and an
    endpoint registry.

    Public API:
        await sri.initiate_call(endpoint_id, announcement="")
        sri.list_endpoints()  -> list[str]
        sri.endpoint_count()  -> int
        sri.get_endpoint(endpoint_id) -> VoiceContext | None
        sri.push_session(endpoint_id) -> str | None
        sri.pop_session(endpoint_id, context_note=None) -> str | None
        sri.set_relay_session(endpoint_id, session_id)
        sri.send_relay_message(endpoint_id, message)
    """

    def __init__(
        self,
        core_processor,
        transcriber            = None,
        vad                    = None,
        piper_voice            = None,
        whisper_model_size:str = 'base.en',
        piper_max_concurrent:int   = 1,
        whisper_max_concurrent:int = 1,
    ):
        super().__init__(
            core_processor          = core_processor,
            vad_threshold           = vad.threshold  if vad else 0.5,
            vad_timeout             = 0.7,
            speaker_id_threshold    = 0.7,
            transcriber             = transcriber,
            piper_voice             = piper_voice,
            whisper_model_size      = whisper_model_size,
            piper_max_concurrent    = piper_max_concurrent,
            whisper_max_concurrent  = whisper_max_concurrent,
        )

        # ── Endpoint registry ─────────────────────────────────────────────────
        # Maps endpoint_id → VoiceContext for the lifetime of the TCP connection.
        # Accessed from both the async frame loop and sync callers (scheduler),
        # so protected by a threading lock.
        self._endpoints:     Dict[str, VoiceContext] = {}
        self._registry_lock: threading.Lock          = threading.Lock()

        # Store the running event loop so send_relay_message can schedule
        # coroutines from sync threads.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Feedback hooks ────────────────────────────────────────────────────────

    async def on_thinking(self, ctx: VoiceContext) -> None:
        """Send THNK — satellite transitions to thinking/processing state."""
        await self._send_frame(ctx, b'THNK')

    async def on_vad_silence_timeout(self, ctx: VoiceContext) -> None:
        # The listening window is over. Any AUD0 still in flight from the
        # satellite (it keeps streaming until THNK reaches it — the whole
        # Whisper duration) belongs to the finished window. Close the gate
        # so that backlog is dropped when the frame loop finally reads it.
        # Reopens on the next MIC1.
        ctx.rx_gate_open = False

    async def on_session_close(self, ctx: VoiceContext) -> None:
        """Send CLOS — session ends but TCP connection stays open."""
        await self._send_frame(ctx, b'CLOS')

    async def on_barge_in(self, ctx: VoiceContext) -> None:
        """Cancel the active LLM response on barge-in."""
        if ctx.session_id is not None:
            try:
                self.core_processor.cancel_active_response(ctx.session_id)
            except Exception:
                log.error("cancel_active_response error",
                          extra={'data': str(ctx.endpoint_id)}, exc_info=True)

    # ── Transport implementation ──────────────────────────────────────────────

    async def _deliver_audio(self, ctx: VoiceContext, audio_f32: np.ndarray, sample_rate: int) -> None:
        """
        Convert Piper float32 output to int16 and stream as TTS0 frames.
        Resamples to INTERNAL_RATE (16kHz) if Piper produced a different rate.
        Chunks at 8192 samples to balance frame overhead vs latency.
        """
        import resampy
        if sample_rate != INTERNAL_RATE:
            audio_f32 = resampy.resample(audio_f32, sample_rate, INTERNAL_RATE)

        audio_int16 = (audio_f32 * 32767.0).astype(np.int16)
        mv          = memoryview(audio_int16.tobytes())
        chunk_bytes = 8192 * 2   # 8192 int16 samples

        writer = self._get_writer(ctx)
        if writer is None:
            return

        for i in range(0, len(mv), chunk_bytes):
            if ctx.interrupt_event.is_set():
                return
            try:
                writer.write(pack_frame(b'TTS0', mv[i:i + chunk_bytes].tobytes()))
            except Exception:
                log.error("TTS0 write error",
                          extra={'data': str(ctx.endpoint_id)}, exc_info=True)
                return
            await writer.drain()
            await asyncio.sleep(0)

    # ── Session metadata ──────────────────────────────────────────────────────

    def _configure_session(self, ctx: VoiceContext, core_session: dict) -> None:
        """Speaker-specific session metadata."""
        super()._configure_session(ctx, core_session)
        # Override interface mode — base defaults to PHONE
        from core.session_state import KEY_INTERFACE_MODE
        core_session[KEY_INTERFACE_MODE] = InterfaceMode.SPEAKER
        core_session['interface']        = InterfaceMode.SPEAKER.value

    # ── Frame send helpers ────────────────────────────────────────────────────

    async def _send_frame(self, ctx: VoiceContext, ftype: bytes, payload: bytes = b'') -> None:
        """Send a control frame to the satellite. Silently swallows write errors."""
        writer = self._get_writer(ctx)
        if writer is None:
            return
        try:
            writer.write(pack_frame(ftype, payload))
            await writer.drain()
        except Exception:
            log.error("Frame write error",
                      extra={'data': f"{ctx.endpoint_id} {ftype}"}, exc_info=True)

    def _get_writer(self, ctx: VoiceContext) -> Optional[asyncio.StreamWriter]:
        """Retrieve the TCP writer stored on the context."""
        return getattr(ctx, '_writer', None)

    # ── Channel lifecycle ─────────────────────────────────────────────────────

    async def _open_channel(self, ctx: VoiceContext) -> None:
        """
        User-initiated wake path.
        Greet the satellite, then send RDY0 to open the microphone.
        rx_paused is held True during the greeting so we don't hear ourselves.
        """
        ctx.rx_gate_open = False
        self.reset_audio_state(ctx)
        ctx.rx_paused = True
        await self._speak_text(ctx, "I'm here")
        await self._send_frame(ctx, b'RDY0')
        ctx.rx_paused = False

    async def _open_channel_silent(self, ctx: VoiceContext, announcement: str) -> None:
        """
        Server-initiated relay path — no greeting, no RDY0.
        The announcement is injected directly into the LLM as the opening turn.
        silent_start=True suppresses the "Working" TTS and first THNK.
        """
        ctx.rx_gate_open = False
        self.reset_audio_state(ctx)
        ctx.rx_paused = True
        await self._contact_core(ctx, announcement, silent_start=True)

    async def _close_session(self, ctx: VoiceContext) -> None:
        """
        Override base _close_session to send CLOS before clearing session_id.
        TCP connection is NOT closed — satellite stays registered and ready
        for the next WAKE.
        """
        if ctx.session_id:
            self.core_processor.close_session(ctx.session_id)
            ctx.session_id = None
        ctx.rx_paused = False
        ctx.rx_gate_open = False
        self.reset_audio_state(ctx)
        await self._send_frame(ctx, b'CLOS')

    # ── Post-LLM response hook ────────────────────────────────────────────────

    async def _after_response(self, ctx: VoiceContext) -> None:
        """
        Called by _contact_core after streaming the full response and
        confirming no hangup was requested. Sends RDY0 to re-open the mic.
        """
        await self._send_frame(ctx, b'RDY0')

    # ── Registry API ─────────────────────────────────────────────────────────

    def _register(self, endpoint_id: str, ctx: VoiceContext) -> None:
        with self._registry_lock:
            self._endpoints[endpoint_id] = ctx
        log.info("Endpoint registered",
                 extra={'data': f"{endpoint_id!r} '{ctx.friendly_name}' total={len(self._endpoints)}"})

    def _unregister(self, endpoint_id: str) -> None:
        with self._registry_lock:
            ctx = self._endpoints.pop(endpoint_id, None)
        name = ctx.friendly_name if ctx else endpoint_id
        log.info("Endpoint unregistered",
                 extra={'data': f"{endpoint_id!r} '{name}' total={len(self._endpoints)}"})

    def list_endpoints(self) -> list:
        with self._registry_lock:
            return list(self._endpoints.keys())

    def endpoint_count(self) -> int:
        with self._registry_lock:
            return len(self._endpoints)

    def get_endpoint(self, endpoint_id: str) -> Optional[VoiceContext]:
        with self._registry_lock:
            return self._endpoints.get(endpoint_id)

    # ── Relay session API ─────────────────────────────────────────────────────

    def push_session(self, endpoint_id: str) -> Optional[str]:
        """
        Suspend the current session by pushing its session_id onto the stack.
        Called by contact_user when initiating a relay to this endpoint.
        Returns the suspended session_id, or None if no session was active.
        """
        ctx = self.get_endpoint(endpoint_id)
        if ctx is None or ctx.session_id is None:
            return None
        ctx.session_stack.append(ctx.session_id)
        suspended      = ctx.session_id
        ctx.session_id = None
        log.info("Session pushed",
                 extra={'data': f"endpoint={endpoint_id} session={suspended}"})
        return suspended

    def pop_session(self, endpoint_id: str, context_note: str = None) -> Optional[str]:
        """
        Resume the most recently suspended session.
        Optionally injects a system-role context note into the session history.
        Called by reply_to_caller when a relay completes.
        Returns the resumed session_id, or None if stack was empty.
        """
        ctx = self.get_endpoint(endpoint_id)
        if ctx is None or not ctx.session_stack:
            return None
        session_id     = ctx.session_stack.pop()
        ctx.session_id = session_id

        if context_note:
            session = self.core_processor.get_session(session_id)
            if session:
                get_history(session).append({
                    'role':    'system',
                    'content': context_note,
                })

        log.info("Session popped",
                 extra={'data': f"endpoint={endpoint_id} session={session_id}"})
        return session_id

    def set_relay_session(self, endpoint_id: str, session_id: str) -> None:
        """
        Activate a relay session as the current session for an endpoint.
        Called by contact_user after creating the relay session.
        """
        ctx = self.get_endpoint(endpoint_id)
        if ctx:
            ctx.session_id = session_id
            log.info("Relay session activated",
                     extra={'data': f"endpoint={endpoint_id} session={session_id}"})

    # ── Server-initiated call ─────────────────────────────────────────────────

    async def initiate_call(self, endpoint_id: str, announcement: str = "") -> bool:
        """
        Push a CALL frame to a connected satellite to start a server-initiated
        session without the user saying the wake word.

        The satellite receives CALL, initiates a WAKE (possibly with the
        announcement as payload), and the server handles it via _open_channel
        or _open_channel_silent.

        Returns True if the CALL was sent, False if not connected or busy.
        """
        ctx = self.get_endpoint(endpoint_id)
        if ctx is None:
            log.warning("initiate_call: not connected",
                        extra={'data': f"{endpoint_id!r}"})
            return False
        if ctx.rx_paused:
            log.warning("initiate_call: endpoint busy",
                        extra={'data': f"{endpoint_id!r}"})
            return False
        try:
            payload = announcement.encode('utf-8') if announcement else b''
            await self._send_frame(ctx, b'CALL', payload)
            log.info("CALL sent", extra={'data': f"{endpoint_id!r}"})
            return True
        except Exception:
            log.error("initiate_call error",
                      extra={'data': f"{endpoint_id!r}"}, exc_info=True)
            return False

    def send_relay_message(self, endpoint_id: str, message: str) -> None:
        """
        Thread-safe entry point for non-async callers (e.g. scheduler).
        Schedules initiate_call on the running event loop.
        """
        if self._loop is None:
            log.error("send_relay_message: no event loop",
                      extra={'data': endpoint_id})
            return
        asyncio.run_coroutine_threadsafe(
            self.initiate_call(endpoint_id, message),
            self._loop,
        )

    # ── Presence registry update ──────────────────────────────────────────────

    def _update_presence(self, ctx: VoiceContext) -> None:
        """
        Update the presence registry after a confirmed voice identification.
        No-op if the core_processor has no presence_registry.
        """
        if not hasattr(self.core_processor, 'presence_registry'):
            return
        registry = self.core_processor.presence_registry
        user_id  = registry.find_user_by_contact(
            'speaker', 'endpoint_id', ctx.endpoint_id
        )
        if not user_id:
            # Try matching by friendly name from speaker profiles
            for uid in registry.all_users():
                name = registry.get_friendly_name(uid)
                if name.lower() == ctx.identified_speaker.lower():
                    user_id = uid
                    break
        if user_id:
            registry.set_last_seen(
                user_id, ctx.endpoint_id, confidence='voice_confirmed'
            )

    # ── Per-connection frame handler ──────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Coroutine for one persistent satellite connection.
        Handles the full connection lifetime including multiple WAKE/CLOS cycles.
        """
        addr = writer.get_extra_info('peername')
        log.info("Satellite connected", extra={'data': str(addr)})

        # Create context — writer stored as plain attribute (not in dataclass)
        ctx          = self.make_context(interface_mode=InterfaceMode.SPEAKER)
        ctx._writer  = writer   # type: ignore[attr-defined]
        ctx.rx_paused = True    # wait for HELO before accepting audio

        try:
            while True:
                ftype, payload = await read_frame(reader)

                # ── HELO: endpoint registration ───────────────────────────────
                if ftype == b'HELO':
                    try:
                        helo          = json.loads(payload.decode('utf-8', errors='replace'))
                        endpoint_id   = helo.get('id',   'unknown')
                        friendly_name = helo.get('name', endpoint_id)
                    except Exception:
                        endpoint_id   = payload.decode('utf-8', errors='replace').strip()
                        friendly_name = endpoint_id

                    ctx.endpoint_id   = endpoint_id
                    ctx.friendly_name = friendly_name
                    self._register(endpoint_id, ctx)
                    ctx.rx_paused = False
                    # No greeting here — satellite is idle until WAKE

                # ── WAKE / OPEN: start or restart a voice session ─────────────
                elif ftype in (b'WAKE', b'OPEN'):
                    announcement = payload.decode('utf-8', errors='replace').strip() if payload else ''
                    if announcement:
                        # Server-initiated relay — inject directly into LLM
                        await self._open_channel_silent(ctx, announcement)
                    else:
                        # User-initiated wake word
                        await self._open_channel(ctx)

                # ── AUD0: microphone audio ────────────────────────────────────
                elif ftype == b'AUD0':
                    if ctx.rx_paused or not ctx.rx_gate_open:
                        log.debug("AUD0 dropped — rx_paused",
                                  extra={'data': str(ctx.endpoint_id)})
                        continue

                    # Satellite sends int16 PCM at 16kHz — decode to float32
                    audio_frame = (
                        np.frombuffer(payload, dtype=np.int16)
                        .astype(np.float32) / 32768.0
                    )
                    # Feed into base VAD/accumulation pipeline
                    await self._process_audio_chunk(ctx, audio_frame)

                # ── INT0: barge-in ────────────────────────────────────────────
                elif ftype == b'INT0':
                    log.info("Barge-in", extra={'data': str(ctx.endpoint_id)})
                    ctx.last_int0_ts = time.monotonic()
                    ctx.interrupt_event.set()
                    # If a speak task is in flight, release rx so we can
                    # receive the next utterance after the interrupt
                    if ctx.speak_task and not ctx.speak_task.done():
                        ctx.rx_paused = False
                    await self.on_barge_in(ctx)
                    ctx.reset_audio()

                # ── MIC1: satellite confirms its mic just (re)opened ─────────
                elif ftype == b'MIC1':
                    # TCP ordering guarantees everything read before this frame
                    # was captured in a previous listening window.
                    ctx.rx_gate_open = True

                # ── STOP: explicit end-of-utterance ───────────────────────────
                elif ftype == b'STOP':
                    # Force transcription immediately — don't wait for silence
                    await self.force_transcribe(ctx)

        except asyncio.IncompleteReadError:
            pass   # clean disconnect

        finally:
            log.info("Satellite disconnected",
                     extra={'data': f"{addr} id={ctx.endpoint_id!r}"})
            if ctx.endpoint_id:
                self._unregister(ctx.endpoint_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Post-transcription speaker ID hook ────────────────────────────────────

    async def _transcribe_buffer(self, ctx: VoiceContext) -> None:
        """
        Override to inject the THNK frame at the right moment and update
        the presence registry after speaker identification.

        Frame ordering:
          1. Base transcribes audio (Whisper)
          2. We send THNK (satellite transitions LISTENING → THINKING)
          3. Base collects speaker ID result
          4. We update presence registry
          5. Base dispatches to _contact_core
          6. _contact_core speaks "Working" (SPEAKING)
          7. _contact_core sends THNK (SPEAKING → THINKING)
          8. LLM streams, TTS plays (SPEAKING per sentence)
          9. _contact_core sends RDY0 (THINKING → LISTENING)
        """
        # Delegate to base — it handles snapshot, Whisper, hallucination filter,
        # close-channel phrase, speaker ID collection, and _contact_core dispatch.
        # We hook in via on_thinking (called before _contact_core) and
        # _after_response (called after RDY0 decision in _contact_core).
        await super()._transcribe_buffer(ctx)

        # Presence registry update — runs after speaker ID is collected
        if ctx.identified_speaker:
            self._update_presence(ctx)

    # ── RDY0 after response ───────────────────────────────────────────────────
    # The base _contact_core sets rx_paused=False at the end but doesn't send
    # RDY0 (it doesn't know about frames). We override _contact_core to hook
    # in the RDY0 send. The cleanest way is to override just the tail.

    async def _contact_core(
        self,
        ctx:          VoiceContext,
        input_text:   str,
        silent_start: bool = False,
    ) -> bool:
        """
        Override to send RDY0 after the LLM response completes (if no hangup).
        All other logic is in the base.
        """
        closed = await super()._contact_core(ctx, input_text, silent_start)
        if not closed:
            # Base set rx_paused=False — now tell the satellite to open its mic
            await self._send_frame(ctx, b'RDY0')
        return closed

    # ── Server entry point ────────────────────────────────────────────────────

    async def run(self, host: str = '0.0.0.0', port: int = 10400) -> None:
        """Start the TCP server. Initialises inference pools inside the running loop."""
        self._loop = asyncio.get_running_loop()
        self._init_pools()

        server = await asyncio.start_server(self._handle_client, host, port)
        addrs  = ', '.join(str(s.getsockname()) for s in server.sockets)
        log.info("Listening", extra={'data': addrs})
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    from core.core import CoreProcessor
    from core.settings import AppConfig

    config = AppConfig.load()
    core   = CoreProcessor(config)
    sri    = SpeakerRemoteInterface(core)
    asyncio.run(sri.run())