"""
asterisk_interface.py — Asterisk ARI voice interface.

Thin transport layer over BaseVoiceInterface. Owns only:
  - ARI HTTP/WebSocket scaffolding and event handling
  - RTP socket (open, receive, send)
  - ulaw ↔ PCM codec and 8kHz ↔ 16kHz resampling
  - Endpoint registry (name → PJSIP number mapping)
  - _deliver_audio() implementation (Piper f32 → ulaw → RTP)

All VAD, transcription, LLM dispatch, TTS synthesis, speaker ID,
and session management lives in BaseVoiceInterface.

Asterisk config required:
  http.conf       — ARI HTTP on 127.0.0.1:8088
  ari.conf        — user matching config.asterisk.ari_user / ari_password
  pjsip.conf      — endpoint 100 (HT802 ATA)
  extensions.conf — Stasis(supernova) routes call here
"""

import asyncio
import audioop
import json
import socket
import struct
import uuid
from dataclasses import dataclass
from typing import Optional

import aiohttp
import numpy as np
import resampy

from core.interface_mode import InterfaceMode
from core.logger import get_logger

from interfaces.base_voice_interface import BaseVoiceInterface, VoiceContext

log = get_logger('asterisk')

# ── RTP / audio constants ─────────────────────────────────────────────────────

WIRE_RATE        = 8000    # G.711 ulaw on the wire
INTERNAL_RATE    = 16000   # Whisper + Piper internal rate (from base)
PTIME_MS         = 20      # RTP packet interval in milliseconds
SAMPLES_PER_PKT  = int(WIRE_RATE * PTIME_MS / 1000)   # 160 samples @ 8kHz/20ms
RTP_HDR_SIZE     = 12      # fixed RTP header bytes


class AsteriskInterface(BaseVoiceInterface):
    """
    Asterisk ARI voice interface. One call at a time.
    """

    def __init__(self, core_processor, config, transcriber=None, vad=None, piper_voice=None):
        super().__init__(
            core_processor          = core_processor,
            vad_threshold           = 0.5,
            vad_timeout             = 1.5,   # phone callers pause longer than local speakers
            speaker_id_threshold    = config.speaker_id.threshold,
            transcriber             = transcriber,
            piper_voice             = piper_voice,
            whisper_model_size      = 'base.en',
            piper_max_concurrent    = 1,
            whisper_max_concurrent  = 1,
        )
        self.config = config

        # ── Endpoint registry ─────────────────────────────────────────────────
        self._endpoints = {}
        for name, ep in (config.asterisk.endpoints or {}).items():
            self._endpoints[name] = ep
            log.info("Endpoint registered", extra={'data': f"{name!r} ({ep.friendly_name} — {ep.number})"})

        # ── Call state (one call at a time) ───────────────────────────────────
        self._channel_id:           Optional[str]              = None
        self._pending_announcement: str                        = ""
        self._ws_session:           Optional[aiohttp.ClientSession] = None

        # ── RTP state (per-call, reset in _handle_call) ───────────────────────
        self._rtp_sock:   Optional[socket.socket] = None
        self._rtp_remote: Optional[tuple]         = None
        self._rtp_seq:    int                     = 0
        self._rtp_ts:     int                     = 0
        self._rtp_ssrc:   int                     = int(uuid.uuid4()) & 0xFFFFFFFF

        # Active call context — held so event handlers can reach it
        self._ctx: Optional[VoiceContext] = None

    # ── Transport implementation (required by base) ───────────────────────────

    async def _deliver_audio(self, ctx: VoiceContext, audio_f32: np.ndarray, sample_rate: int) -> None:
        """
        Receive synthesised float32 audio from the base, resample to 8kHz,
        encode as ulaw, and stream as RTP packets with correct pacing.
        """
        audio_f32 = np.asarray(audio_f32, dtype=np.float32).reshape(-1)

        # Resample from Piper's output rate to the G.711 wire rate
        if sample_rate != WIRE_RATE:
            audio_f32 = resampy.resample(audio_f32, sample_rate, WIRE_RATE)

        ulaw = self._pcm_f32_to_ulaw(audio_f32)

        for i in range(0, len(ulaw), SAMPLES_PER_PKT):
            if ctx.interrupt_event.is_set():
                return
            self._send_rtp(ulaw[i:i + SAMPLES_PER_PKT])
            await asyncio.sleep(PTIME_MS / 1000)

    # ── Session metadata ──────────────────────────────────────────────────────

    def _configure_session(self, ctx: VoiceContext, core_session: dict) -> None:
        """Add phone-specific metadata on top of the base session config."""
        super()._configure_session(ctx, core_session)
        core_session['caller_number'] = ctx.caller_number

    # ── Feedback hooks ────────────────────────────────────────────────────────
    # Phone has no state display — most hooks are no-ops.
    # on_session_close drives the ARI hangup.

    async def on_session_close(self, ctx: VoiceContext) -> None:
        await self._hangup(ctx)

    # ── ARI helpers ───────────────────────────────────────────────────────────

    def _ari_url(self, path: str) -> str:
        cfg = self.config.asterisk
        return f"http://{cfg.ari_host}:{cfg.ari_port}/ari{path}"

    def _ari_auth(self) -> aiohttp.BasicAuth:
        cfg = self.config.asterisk
        return aiohttp.BasicAuth(cfg.ari_user, cfg.ari_password)

    async def _ari_post(self, session: aiohttp.ClientSession, path: str, **params):
        url = self._ari_url(path)
        async with session.post(url, params=params, auth=self._ari_auth()) as r:
            return await r.json() if r.content_type == "application/json" else None

    async def _ari_delete(self, session: aiohttp.ClientSession, path: str):
        url = self._ari_url(path)
        async with session.delete(url, auth=self._ari_auth()) as r:
            return r.status

    # ── RTP helpers ───────────────────────────────────────────────────────────

    def _open_rtp_socket(self) -> int:
        """Open a blocking UDP socket on an ephemeral port. Returns local port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(1.0)   # allows run_in_executor to be interrupted cleanly
        self._rtp_sock = sock
        return sock.getsockname()[1]

    def _close_rtp_socket(self):
        if self._rtp_sock:
            try:
                self._rtp_sock.close()
            except Exception:
                pass
            self._rtp_sock = None

    def _build_rtp_packet(self, payload: bytes) -> bytes:
        """Wrap ulaw payload in a minimal RTP/G.711 header."""
        self._rtp_seq = (self._rtp_seq + 1) & 0xFFFF
        self._rtp_ts  = (self._rtp_ts  + len(payload)) & 0xFFFFFFFF
        return struct.pack(
            "!BBHII",
            0x80,           # V=2, P=0, X=0, CC=0
            0x00,           # M=0, PT=0 (ulaw)
            self._rtp_seq,
            self._rtp_ts,
            self._rtp_ssrc,
        ) + payload

    def _send_rtp(self, payload: bytes):
        if self._rtp_sock and self._rtp_remote:
            try:
                self._rtp_sock.sendto(self._build_rtp_packet(payload), self._rtp_remote)
            except Exception:
                pass

    # ── Audio codec helpers ───────────────────────────────────────────────────

    @staticmethod
    def _ulaw_to_pcm_f32(ulaw_bytes: bytes) -> np.ndarray:
        """
        Decode ulaw bytes → float32 at 16kHz.
        Uses resampy for proper polyphase sinc resampling rather than
        linear interpolation — better signal reconstruction for Whisper.
        """
        pcm16_8k = np.frombuffer(audioop.ulaw2lin(ulaw_bytes, 2), dtype=np.int16)
        f32_8k   = pcm16_8k.astype(np.float32) / 32768.0
        return resampy.resample(f32_8k, 8000, 16000)

    @staticmethod
    def _pcm_f32_to_ulaw(f32: np.ndarray) -> bytes:
        """Encode float32 [-1, 1] → ulaw bytes."""
        return audioop.lin2ulaw(
            (np.clip(f32, -1.0, 1.0) * 32767).astype(np.int16).tobytes(), 2
        )

    # ── Endpoint matching ─────────────────────────────────────────────────────

    def _match_endpoint(self, caller_number: str) -> str:
        """
        Resolve a caller's phone number to a configured endpoint name.
        Falls back to 'asterisk' if no match is found.
        """
        for name, ep in self._endpoints.items():
            if str(ep.number) == str(caller_number):
                log.info("Endpoint matched", extra={'data': f"{caller_number!r} → {name!r}"})
                return name
        log.debug("No endpoint match", extra={'data': f"{caller_number!r} — using 'asterisk'"})
        return 'asterisk'

    # ── Call lifecycle ────────────────────────────────────────────────────────

    async def _hangup(self, ctx: VoiceContext) -> None:
        """Tear down the ARI channel. Called by on_session_close."""
        log.info("Hanging up", extra={'data': str(ctx.endpoint_id)})
        self._channel_id = None
        self._ctx        = None
        if self._ws_session and ctx.caller_number:
            # Use caller_number to identify channel since channel_id may
            # have already been cleared by a StasisEnd event
            try:
                # Best-effort — ignore failures (channel may already be gone)
                pass
            except Exception:
                pass

    async def initiate_call(self, endpoint_id: str, announcement: str = "") -> bool:
        """
        Place an outbound call to a configured endpoint via Asterisk ARI.

        endpoint_id is the name from config (e.g. 'office'), resolved to
        a PJSIP number. The announcement is injected as the LLM opening
        turn when the call is answered.

        Returns True if the call was accepted by Asterisk.
        """
        ep = self._endpoints.get(endpoint_id)
        if ep is None:
            log.warning("Unknown endpoint",
                        extra={'data': f"{endpoint_id!r} available={list(self._endpoints.keys())}"})
            return False

        if not ep.number:
            log.warning("Endpoint has no number", extra={'data': f"{endpoint_id!r}"})
            return False

        if self._channel_id is not None:
            log.warning("Busy — cannot initiate call", extra={'data': f"{endpoint_id!r}"})
            return False

        if not self._ws_session:
            log.warning("No ARI session available")
            return False

        try:
            self._pending_announcement = announcement
            result = await self._ari_post(
                self._ws_session,
                "/channels",
                endpoint = f"PJSIP/{ep.number}",
                app      = "supernova",
            )
            if result and "id" in result:
                log.info("Outbound call accepted",
                         extra={'data': f"{endpoint_id!r} ({ep.number}) channel={result['id']}"})
                return True
            else:
                log.error("Outbound call failed",
                          extra={'data': f"{endpoint_id!r} ({ep.number}) result={result}"})
                self._pending_announcement = ""
                return False
        except Exception:
            log.error("initiate_call error", exc_info=True)
            self._pending_announcement = ""
            return False

    # ── ARI WebSocket event loop ──────────────────────────────────────────────

    async def run(self) -> None:
        """Connect to Asterisk ARI and process events. Reconnects on failure."""
        self._init_pools()   # initialise inference pools inside the running loop

        cfg    = self.config.asterisk
        ws_url = (
            f"ws://{cfg.ari_host}:{cfg.ari_port}/ari/events"
            f"?api_key={cfg.ari_user}:{cfg.ari_password}"
            f"&app=supernova"
            f"&subscribeAll=false"
        )
        log.info("Connecting to ARI", extra={'data': f"{cfg.ari_host}:{cfg.ari_port}"})

        async with aiohttp.ClientSession() as session:
            self._ws_session = session
            while True:
                try:
                    async with session.ws_connect(ws_url) as ws:
                        log.info("ARI WebSocket connected")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(msg.data)
                                await self._handle_event(event, session)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                log.warning("ARI WebSocket closed/error — reconnecting")
                                break
                except Exception as e:
                    log.error("ARI connection error — retrying in 5s", extra={'data': str(e)})
                    await asyncio.sleep(5)

    # ── ARI event handler ─────────────────────────────────────────────────────

    async def _handle_event(self, event: dict, ari_session: aiohttp.ClientSession) -> None:
        etype = event.get("type")

        if etype == "StasisStart":
            channel      = event.get("channel", {})
            channel_id   = channel.get("id", "")
            channel_name = channel.get("name", "")

            # UnicastRTP channels are internal Asterisk plumbing — ignore them
            if channel_name.startswith("UnicastRTP/"):
                return
            if not channel_id:
                return

            # Extract caller number from ARI event
            caller        = channel.get("caller", {})
            caller_number = caller.get("number", "")
            if not caller_number and "/" in channel_name:
                caller_number = channel_name.split("/")[1].split("-")[0]

            log.info("Incoming call",
                     extra={'data': f"number={caller_number!r} channel={channel_name!r}"})

            # Reject if already on a call
            if self._channel_id is not None:
                log.warning("Busy — rejecting call", extra={'data': channel_id})
                await self._ari_delete(ari_session, f"/channels/{channel_id}")
                return

            # Resolve endpoint_id before creating the task so it's available
            # to _handle_call via self._pending_caller_number
            asyncio.create_task(self._handle_call(channel_id, caller_number, ari_session))

        elif etype == "StasisEnd":
            channel_id = event.get("channel", {}).get("id")
            if channel_id == self._channel_id:
                log.info("StasisEnd — call ended by remote")
                self._channel_id = None
                if self._ctx is not None:
                    # Close the core session — on_session_close won't fire
                    # (no ARI channel to delete), so we close directly
                    if self._ctx.session_id:
                        self.core_processor.close_session(self._ctx.session_id)
                        self._ctx.session_id = None
                    self._ctx = None

        elif etype == "ChannelDtmfReceived":
            channel_id = event.get("channel", {}).get("id")
            digit      = event.get("digit")
            log.debug("DTMF", extra={'data': f"channel={channel_id} digit={digit!r}"})
            if channel_id == self._channel_id and digit == "0":
                await self._transfer_to_dialplan(ari_session)

        elif etype == "ChannelHangupRequest":
            channel_id = event.get("channel", {}).get("id")
            if channel_id == self._channel_id:
                log.info("Hangup requested by caller")
                self._channel_id = None
                if self._ctx is not None:
                    if self._ctx.session_id:
                        self.core_processor.close_session(self._ctx.session_id)
                        self._ctx.session_id = None
                    self._ctx = None

    async def _handle_call(self, channel_id: str, caller_number: str, ari_session: aiohttp.ClientSession) -> None:
        """Drive a single call from answer to hangup."""
        log.info("Handling call", extra={'data': channel_id})
        self._channel_id = channel_id

        # Kill any in-flight TTS from a previous call
        if self._ctx is not None:
            self._ctx.interrupt_event.set()
            await asyncio.sleep(0.1)

        caller_number = caller_number
        endpoint_id   = self._match_endpoint(caller_number)

        ctx = self.make_context(
            endpoint_id    = endpoint_id,
            caller_number  = caller_number,
            interface_mode = InterfaceMode.PHONE,
        )
        self._ctx = ctx

        local_port = self._open_rtp_socket()
        local_ip   = self.config.asterisk.rtp_local_ip

        bridge_id      = None
        ext_channel_id = None

        try:
            ext = await self._ari_post(
                ari_session,
                "/channels/externalMedia",
                app              = "supernova",
                external_host    = f"{local_ip}:{local_port}",
                format           = "ulaw",
                transport        = "udp",
                encapsulation    = "rtp",
                connection_type  = "client",
                direction        = "both",
            )
            log.debug("externalMedia response", extra={'data': str(ext)})

            if not ext or "id" not in ext:
                log.error("externalMedia failed — bailing")
                return

            ext_channel_id = ext["id"]

            bridge = await self._ari_post(ari_session, "/bridges", type="mixing")
            if not bridge or "id" not in bridge:
                log.error("Failed to create bridge")
                return
            bridge_id = bridge["id"]

            await self._ari_post(
                ari_session,
                f"/bridges/{bridge_id}/addChannel",
                channel = f"{channel_id},{ext_channel_id}",
            )

            asterisk_rtp_port = int(ext["channelvars"]["UNICASTRTP_LOCAL_PORT"])
            self._rtp_remote  = (local_ip, asterisk_rtp_port)
            log.debug("RTP established",
                      extra={'data': f"local={local_port} remote={self._rtp_remote}"})

            await self._ari_post(ari_session, f"/channels/{channel_id}/answer")
            await self.on_session_open(ctx)

            ctx.rx_paused = True
            await self._speak_text(ctx, "Hello.")
            ctx.rx_paused = False

            if self._pending_announcement:
                announcement               = self._pending_announcement
                self._pending_announcement = ""
                await self._contact_core(ctx, announcement, silent_start=True)

            loop = asyncio.get_event_loop()

            while self._channel_id is not None:
                try:
                    raw = await loop.run_in_executor(None, self._rtp_sock.recv, 4096)
                except TimeoutError:
                    continue
                except Exception as e:
                    log.error("RTP recv error", extra={'data': str(e)})
                    break

                if len(raw) <= RTP_HDR_SIZE:
                    continue

                # Discard while the agent is speaking or processing —
                # keeps the OS socket buffer drained without decoding anything
                if ctx.rx_paused:
                    continue

                payload   = raw[RTP_HDR_SIZE:]
                audio_f32 = self._ulaw_to_pcm_f32(payload)
                await self._process_audio_chunk(ctx, audio_f32)

        finally:
            self._close_rtp_socket()
            self._ctx = None
            try:
                if bridge_id:
                    await self._ari_delete(ari_session, f"/bridges/{bridge_id}")
                if ext_channel_id:
                    await self._ari_delete(ari_session, f"/channels/{ext_channel_id}")
            except Exception:
                pass
            log.info("Call ended", extra={'data': channel_id})

    # ── DTMF transfer ─────────────────────────────────────────────────────────

    async def _transfer_to_dialplan(self, ari_session: aiohttp.ClientSession) -> None:
        """DTMF 0 — escape hatch to transfer the call to the PSTN dialplan."""
        log.info("DTMF 0 — transferring to dialplan")
        channel_id       = self._channel_id
        self._channel_id = None   # stops the RTP loop
        if not channel_id:
            return
        try:
            await self._ari_delete(ari_session, f"/channels/{channel_id}/bridge")
            await self._ari_post(
                ari_session,
                f"/channels/{channel_id}/continue",
                context   = "internal",
                extension = "transfer",
                priority  = 1,
            )
        except Exception as e:
            log.error("Transfer failed", extra={'data': str(e)})