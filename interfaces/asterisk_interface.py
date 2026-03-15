import asyncio
import audioop
import json
import re
import socket
import struct
import threading
import time
import uuid
from typing import Optional

import aiohttp
import numpy as np

from piper import PiperVoice, SynthesisConfig
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel

# ============================================================
# AsteriskInterface
# ============================================================
# Sibling to VoiceRemoteInterface — handles calls via Asterisk ARI.
# One call at a time. Audio via RTP external media bridge.
#
# Asterisk config required:
#   http.conf       — ARI HTTP on 127.0.0.1:8088
#   ari.conf        — user matching config.asterisk.ari_user / ari_password
#   pjsip.conf      — endpoint 100 (HT802 ATA)
#   extensions.conf — Stasis(supernova) routes call here
# ============================================================

PTIME_MS        = 20      # RTP packet interval in milliseconds
SAMPLE_RATE     = 8000    # ulaw on the wire (G.711)
AGENT_RATE      = 16000   # Whisper + Piper internal rate
SAMPLE_WIDTH    = 2       # int16
RTP_HDR_SIZE    = 12      # fixed RTP header bytes to strip
VAD_MIN_SAMPLES = 1600     # Silero VAD minimum chunk size


class AsteriskInterface:

    def __init__(self, core_processor, config, transcriber=None, vad=None):
        self.core_processor = core_processor
        self.config = config  # full AppConfig — we use config.asterisk

        # ASR / VAD — shared instances passed in from main, or own if not provided
        self.vad = vad or VoiceActivityDetector(threshold=0.5, frame_rate=AGENT_RATE)
        self.transcriber = transcriber or WhisperModel(model_size_or_path="base.en")

        # TTS (Piper) — own instance + lock
        self._piper_lock = threading.Lock()
        self.voice = PiperVoice.load(
            "./libs/voices/glados_piper_medium.onnx",
            use_cuda=False,
        )
        self.piper_syn_config = SynthesisConfig(
            volume=1.0,
            length_scale=1.0,
            noise_scale=1.0,
            noise_w_scale=1.0,
            normalize_audio=False,
        )

        # Sentence splitter (same regex as VoiceRemoteInterface)
        self.sentence_endings = re.compile(
            r'(?:(?<=[!?])(?:\s+|$))'
            r'|(?:(?<=\.)(?!\d)(?:\s+|$))'
            r'|(?:(?<=[,;])(?:\s+|$))'
            r'|[\r\n]+'
        )

        # Session state — reset per call in _handle_call
        self.session_id: Optional[str] = None
        self.channel_id: Optional[str] = None
        self.frames_np     = np.array([], dtype=np.float32)
        self.recording     = False
        self.last_voice_ts: Optional[float] = None
        self.vad_timeout   = 1.5
        self.rx_paused     = False
        self.interrupt_event = asyncio.Event()
        self.close_channel_phrase = "finish conversation"

        # RTP
        self._rtp_sock: Optional[socket.socket] = None
        self._rtp_remote: Optional[tuple] = None
        self._rtp_seq  = 0
        self._rtp_ts   = 0
        self._rtp_ssrc = int(uuid.uuid4()) & 0xFFFFFFFF

        # ARI websocket session (aiohttp)
        self._ws_session: Optional[aiohttp.ClientSession] = None

    # ----------------------------------------------------------
    # ARI helpers
    # ----------------------------------------------------------

    def _ari_url(self, path: str) -> str:
        cfg = self.config.asterisk
        return f"http://{cfg.ari_host}:{cfg.ari_port}/ari{path}"

    def _ari_auth(self):
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

    # ----------------------------------------------------------
    # RTP helpers
    # ----------------------------------------------------------

    def _open_rtp_socket(self) -> int:
        """Open a blocking UDP socket on an ephemeral port, return port number."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(1.0)   # blocking with timeout so run_in_executor can be interrupted
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
        """Wrap ulaw payload in a minimal RTP header."""
        self._rtp_seq = (self._rtp_seq + 1) & 0xFFFF
        self._rtp_ts  = (self._rtp_ts + len(payload)) & 0xFFFFFFFF
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

    # ----------------------------------------------------------
    # Audio conversion helpers
    # ----------------------------------------------------------

    @staticmethod
    def _ulaw_to_pcm16(ulaw_bytes: bytes) -> np.ndarray:
        """Convert ulaw bytes → int16 numpy array at 8kHz."""
        return np.frombuffer(audioop.ulaw2lin(ulaw_bytes, 2), dtype=np.int16)

    @staticmethod
    def _resample_8k_to_16k(arr_int16: np.ndarray) -> np.ndarray:
        """2x upsample by linear interpolation."""
        f32 = arr_int16.astype(np.float32) / 32768.0
        return np.interp(
            np.arange(0, len(f32), 0.5),
            np.arange(len(f32)),
            f32,
        ).astype(np.float32)

    @staticmethod
    def _pcm_f32_to_ulaw(f32: np.ndarray) -> bytes:
        """Convert float32 [-1,1] array → ulaw bytes."""
        return audioop.lin2ulaw(
            (np.clip(f32, -1.0, 1.0) * 32767).astype(np.int16).tobytes(), 2
        )

    # ----------------------------------------------------------
    # VAD / transcription
    # ----------------------------------------------------------

    def _add_frames(self, frame_np: np.ndarray):
        if self.frames_np.size == 0:
            self.frames_np = frame_np.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, frame_np))

    async def _transcribe_and_respond(self):
        if self.frames_np.size == 0:
            return

        min_samples = int(0.2 * AGENT_RATE)
        if self.frames_np.size < min_samples:
            print(f"[asterisk] Ignoring short buffer ({self.frames_np.size} samples)")
            self.frames_np = np.array([], dtype=np.float32)
            return

        try:
            segments, _ = self.transcriber.transcribe(self.frames_np)
            self.frames_np = np.array([], dtype=np.float32)
        except Exception as e:
            print(f"[asterisk] ASR error: {e}")
            self.frames_np = np.array([], dtype=np.float32)
            return

        if not segments:
            return

        text = " ".join(seg.text for seg in segments).strip()
        print(f"[asterisk] Transcription: '{text}'")

        if not text or re.fullmatch(r'[\s.…]+', text):
            print("[asterisk] Ignoring Whisper hallucination")
            return

        if self.close_channel_phrase in text.lower():
            await self._hangup()
            return

        await self._contact_core(text)

    # ----------------------------------------------------------
    # Core processor interaction + TTS streaming
    # ----------------------------------------------------------

    async def _contact_core(self, input_text: str):
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)
            self.rx_paused = True
            await self._speak_text("Working")

        print(f"[asterisk] Processing: {input_text}")
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={
                "input_text": input_text,
                "session_id": self.session_id,
                "is_voice": False,
            },
            daemon=True,
        )
        thread.start()

        session = self.core_processor.get_session(self.session_id)
        buffer = ""
        self.rx_paused = True

        while True:
            chunk = await asyncio.to_thread(session["response_queue"].get)
            if chunk is None:
                break
            buffer += chunk
            sentences = self.sentence_endings.split(buffer)
            for sent in sentences[:-1]:
                sent = re.sub(r'[*#`_~]', '', sent).strip()
                if sent and not self.interrupt_event.is_set():
                    await self._speak_text(sent)
            buffer = sentences[-1]

        if self.interrupt_event.is_set():
            buffer = ""

        if buffer.strip():
            await self._speak_text(re.sub(r'[*#`_~]', '', buffer).strip())

        session_obj = self.core_processor.get_session(self.session_id)
        close = session_obj["close_voice_channel"].is_set()

        if close:
            session_obj["close_voice_channel"].clear()
            await self._hangup()
        else:
            self.rx_paused = False

    async def _speak_text(self, text: str):
        if not text.strip():
            return

        prev_rx = self.rx_paused
        self.rx_paused = True

        try:
            def synth():
                with self._piper_lock:
                    return [
                        (chunk.sample_rate, chunk.audio_float_array.copy())
                        for chunk in self.voice.synthesize(text, syn_config=self.piper_syn_config)
                    ]

            try:
                piper_chunks = await asyncio.to_thread(synth)
            except Exception as e:
                print(f"[asterisk] Piper error: {e}")
                return

            samples_per_packet = int(SAMPLE_RATE * PTIME_MS / 1000)  # 160 @ 8kHz/20ms

            for sr, audio_f32 in piper_chunks:
                if self.interrupt_event.is_set():
                    return

                audio_f32 = np.asarray(audio_f32, dtype=np.float32).reshape(-1)

                # Normalise
                rms = float(np.sqrt(np.mean(audio_f32 ** 2))) if audio_f32.size else 0.0
                if rms > 1e-8:
                    audio_f32 *= (0.2 / rms)
                audio_f32 = np.clip(audio_f32, -1.0, 1.0)

                # Resample Piper rate → 8kHz for ulaw
                if sr != SAMPLE_RATE:
                    import resampy
                    audio_f32 = resampy.resample(audio_f32, sr, SAMPLE_RATE)

                ulaw = self._pcm_f32_to_ulaw(audio_f32)
                for i in range(0, len(ulaw), samples_per_packet):
                    if self.interrupt_event.is_set():
                        return
                    self._send_rtp(ulaw[i:i + samples_per_packet])
                    await asyncio.sleep(PTIME_MS / 1000)
        finally:
            self.rx_paused = prev_rx

    # ----------------------------------------------------------
    # Call lifecycle
    # ----------------------------------------------------------

    async def _hangup(self):
        print("[asterisk] Hanging up.")
        self.session_id = None
        self.rx_paused = False
        if self._ws_session and self.channel_id:
            try:
                await self._ari_delete(self._ws_session, f"/channels/{self.channel_id}")
            except Exception:
                pass

    async def _handle_call(self, channel_id: str, session: aiohttp.ClientSession):
        """Drive a single call from answer to hangup."""
        print(f"[asterisk] Handling call on channel {channel_id}")
        self.channel_id    = channel_id
        self.session_id    = None
        self.frames_np     = np.array([], dtype=np.float32)
        self.recording     = False
        self.last_voice_ts = None
        self.rx_paused     = False
        self.interrupt_event.clear()

        local_port = self._open_rtp_socket()
        local_ip   = self.config.asterisk.rtp_local_ip

        bridge_id      = None
        ext_channel_id = None

        try:
            # Create external media channel — Asterisk will send RTP to local_ip:local_port
            ext = await self._ari_post(
                session,
                "/channels/externalMedia",
                app="supernova",
                external_host=f"{local_ip}:{local_port}",
                format="ulaw",
                transport="udp",
                encapsulation="rtp",
                connection_type="client",
                direction="both",
            )
            print(f"[asterisk] externalMedia response: {ext}")

            if not ext or "id" not in ext:
                print(f"[asterisk] externalMedia failed — bailing")
                return

            ext_channel_id = ext["id"]

            # Bridge the call channel and external media channel
            bridge = await self._ari_post(session, "/bridges", type="mixing")
            if not bridge or "id" not in bridge:
                print("[asterisk] Failed to create bridge")
                return
            bridge_id = bridge["id"]

            result = await self._ari_post(
                session,
                f"/bridges/{bridge_id}/addChannel",
                channel=f"{channel_id},{ext_channel_id}",
            )
            print(f"[asterisk] addChannel result: {result}")

            # Asterisk sends RTP to our socket — we send back to the same address
            # The UnicastRTP channel name (extract):
            asterisk_rtp_port = int(ext["channelvars"]["UNICASTRTP_LOCAL_PORT"])
            self._rtp_remote = (local_ip, asterisk_rtp_port)
            print(f"[asterisk] RTP remote: {self._rtp_remote}, local port: {local_port}")

            # Answer the original channel
            await self._ari_post(session, f"/channels/{channel_id}/answer")

            # Greet the caller
            self.rx_paused = True
            await self._speak_text("Hello, I'm here.")
            self.rx_paused = False

            # RTP receive loop
            loop = asyncio.get_event_loop()
            audio_buffer = np.array([], dtype=np.float32)

            # small loopback so we prepend detected sound onto transcription:
            audio_buffer  = np.array([], dtype=np.float32)
            lookback      = np.array([], dtype=np.float32)
            LOOKBACK_SIZE = AGENT_RATE  # 1 second of lookback

            while self.channel_id is not None:
                try:
                    raw = await loop.run_in_executor(None, self._rtp_sock.recv, 4096)
                except TimeoutError:
                    # No RTP packet in 1s — check if call is still active and loop
                    continue
                except Exception as e:
                    print(f"[asterisk] RTP recv error: {e}")
                    break

                if len(raw) <= RTP_HDR_SIZE:
                    continue

                if self.rx_paused:
                    # attempting to keep consuming audio packets while speaking so we don't accumulate and dump on the ASR:
                    audio_buffer = np.array([], dtype=np.float32)
                    self.frames_np = np.array([], dtype=np.float32)
                    self.recording = False
                    self.last_voice_ts = None
                    continue

                payload   = raw[RTP_HDR_SIZE:]
                pcm16_8k  = self._ulaw_to_pcm16(payload)
                audio_f32 = self._resample_8k_to_16k(pcm16_8k)

                rms = float(np.sqrt(np.mean(audio_f32 ** 2)))
                # debug - check levels incoming:
                #print(f"[asterisk] RTP RMS: {rms:.4f}")

                # Buffer until we have enough samples for VAD
                audio_buffer = np.concatenate([audio_buffer, audio_f32])
                if len(audio_buffer) < VAD_MIN_SAMPLES:
                    continue

                chunk        = audio_buffer[:VAD_MIN_SAMPLES]
                audio_buffer = audio_buffer[VAD_MIN_SAMPLES:]

                if self.vad(audio_frame=chunk):
                    if not self.recording:
                        self.recording = True
                        self.interrupt_event.clear()
                        # Prepend the lookback buffer so we don't lose the start of the utterance
                        if lookback.size > 0:
                            self._add_frames(lookback)
                    self.last_voice_ts = time.monotonic()
                    self._add_frames(chunk)
                elif self.recording:
                    # Keep adding frames during brief silences so we don't lose mid-sentence gaps
                    self._add_frames(chunk)
                    if (self.last_voice_ts is not None and
                            (time.monotonic() - self.last_voice_ts) > self.vad_timeout):
                        if self.frames_np.size >= int(0.3 * AGENT_RATE):  # 0.3s minimum
                            await self._transcribe_and_respond()
                        else:
                            print(f"[asterisk] Discarding short buffer ({self.frames_np.size} samples)")
                            self.frames_np = np.array([], dtype=np.float32)
                        self.recording     = False
                        self.last_voice_ts = None
                # After the VAD block, always update lookback with latest chunk:
                lookback = np.concatenate([lookback, chunk])
                if len(lookback) > LOOKBACK_SIZE:
                    lookback = lookback[-LOOKBACK_SIZE:]

        finally:
            self._close_rtp_socket()
            try:
                if bridge_id:
                    await self._ari_delete(session, f"/bridges/{bridge_id}")
                if ext_channel_id:
                    await self._ari_delete(session, f"/channels/{ext_channel_id}")
            except Exception:
                pass
            print(f"[asterisk] Call ended on channel {channel_id}")

    # ----------------------------------------------------------
    # ARI WebSocket event loop
    # ----------------------------------------------------------

    async def run(self):
        cfg = self.config.asterisk
        ws_url = (
            f"ws://{cfg.ari_host}:{cfg.ari_port}/ari/events"
            f"?api_key={cfg.ari_user}:{cfg.ari_password}"
            f"&app=supernova"
            f"&subscribeAll=false"
        )

        print(f"[asterisk] Connecting to ARI at {cfg.ari_host}:{cfg.ari_port}")

        async with aiohttp.ClientSession() as session:
            self._ws_session = session
            while True:
                try:
                    async with session.ws_connect(ws_url) as ws:
                        print("[asterisk] ARI WebSocket connected.")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(msg.data)
                                await self._handle_event(event, session)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                print("[asterisk] WebSocket closed/error, reconnecting...")
                                break
                except Exception as e:
                    print(f"[asterisk] ARI connection error: {e}, retrying in 5s...")
                    await asyncio.sleep(5)

    async def _transfer_to_dialplan(self, session: aiohttp.ClientSession):
        # allows transferring to dialtone
        print("[asterisk] DTMF 0 — transferring to dialplan")
        channel_id = self.channel_id   # save before nulling
        self.channel_id = None         # stop the RTP loop
        if not channel_id:
            return
        try:
            # Must remove from bridge before continuing into dialplan
            await self._ari_delete(session, f"/channels/{channel_id}/bridge")
            await self._ari_post(
                session,
                f"/channels/{channel_id}/continue",
                context="internal",
                extension="transfer",
                priority=1,
            )
        except Exception as e:
            print(f"[asterisk] Transfer failed: {e}")

    async def _handle_event(self, event: dict, session: aiohttp.ClientSession):
        etype = event.get("type")
        # debugging message types:
        #if etype not in ("ChannelHangupRequest",):  # filter noise
        #    print(f"[asterisk] Event: {etype}")

        if etype == "StasisStart":
            channel      = event.get("channel", {})
            channel_id   = channel.get("id", "")
            channel_name = channel.get("name", "")

            # Ignore externalMedia pseudo-channels (UnicastRTP/...)
            if channel_name.startswith("UnicastRTP/"):
                return
            if not channel_id:
                return

            asyncio.create_task(self._handle_call(channel_id, session))

        elif etype == "StasisEnd":
            channel_id = event.get("channel", {}).get("id")
            if channel_id == self.channel_id:
                print("[asterisk] StasisEnd — call ended by remote.")
                self.channel_id = None

        # we can dial 0 for an outside line:
        elif etype == "ChannelDtmfReceived":
            channel_id = event.get("channel", {}).get("id")
            digit = event.get("digit")
            
            # debug:
            print(f"[asterisk] DTMF channel_id={channel_id}, digit={digit}")

            if channel_id == self.channel_id and digit == "0":
                await self._transfer_to_dialplan(session)

        elif etype == "ChannelHangupRequest":
            channel_id = event.get("channel", {}).get("id")
            if channel_id == self.channel_id:
                print("[asterisk] Hangup requested by caller.")
                self.channel_id = None