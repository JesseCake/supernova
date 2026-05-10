"""
base_voice_interface.py — Transport-agnostic voice interface engine.

All speech processing logic lives here: VAD accumulation, transcription,
LLM dispatch, TTS synthesis, speaker identification, and feedback hooks.
Transport subclasses implement only what is specific to their medium.

Subclass contract
─────────────────
Must implement:
    async def _deliver_audio(self, ctx: VoiceContext, audio_f32: np.ndarray, sample_rate: int) -> None

May override (all are no-ops in the base):
    async def on_vad_triggered(self, ctx)       # voice onset detected
    async def on_vad_silence_timeout(self, ctx) # silence crossed — about to transcribe
    async def on_thinking(self, ctx)            # dispatched to LLM, waiting for response
    async def on_speaking_start(self, ctx)      # TTS about to play
    async def on_speaking_end(self, ctx)        # TTS finished, ready for input
    async def on_session_open(self, ctx)        # channel/call opened
    async def on_session_close(self, ctx)       # channel/call closed

Audio input
───────────
Subclasses feed decoded, normalised float32 audio at INTERNAL_RATE (16 kHz)
into the pipeline by calling:
    await self._process_audio_chunk(ctx, chunk_f32)

The base handles VAD, lookback, accumulation, silence timeout, and
transcription trigger. The subclass is responsible for any codec conversion
(e.g. ulaw → PCM, 8kHz → 16kHz) before calling _process_audio_chunk.

Context object
──────────────
Each active call/connection is represented by a VoiceContext dataclass.
The base is stateless — all mutable state lives in the context.
Subclasses create a VoiceContext at call/connection start and pass it
through every base method. This makes the base naturally concurrent-safe
for multi-connection transports (e.g. SpeakerRemoteInterface).
"""

import asyncio
import os
import re
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import resampy

from piper import PiperVoice, SynthesisConfig
from faster_whisper import WhisperModel

from interfaces.vad import VoiceActivityDetector

from core.interface_mode import InterfaceMode
from core.session_state import (
    KEY_INTERFACE_MODE,
    hangup_requested, clear_hangup,
    get_response_queue,
)
from core.speaker_id import SpeakerIdentifier, load_profiles

from core.logger import get_logger
log = get_logger('base_voice')

# ── Audio constants ───────────────────────────────────────────────────────────

INTERNAL_RATE    = 16000   # sample rate for all internal audio (Whisper + Piper)
VAD_MIN_SAMPLES  = 1600    # minimum chunk size for Silero VAD at 16kHz (~100ms)
LOOKBACK_SECONDS = 1.0     # lookback buffer duration — prepended on voice onset
                            # so the start of an utterance is never clipped


# ── Inference pool ────────────────────────────────────────────────────────────

class _InferencePool:
    """
    Semaphore-gated wrapper around a single shared inference model.

        async with pool as model:
            result = model.infer(...)

    Blocks until a slot is available. max_concurrent=1 serialises all
    inference, which is correct for low-traffic single-GPU use.
    """

    def __init__(self, model, max_concurrent: int = 1):
        self.model = model
        self._sem  = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self):
        await self._sem.acquire()
        return self.model

    async def __aexit__(self, *_):
        self._sem.release()


# ── Per-call context ──────────────────────────────────────────────────────────

@dataclass
class VoiceContext:
    """
    All mutable state for one active call or connection.

    Created by the subclass at call/connection start. Passed into every
    base method. The base never holds references to contexts — all state
    is in the context itself, making multi-call operation safe.

    Fields marked '# subclass' are set by the subclass before or during
    the call; the base reads but does not set them (except session_id).
    """

    # ── Identity (set by subclass before first _contact_core) ────────────────
    endpoint_id:        Optional[str]   = None   # logical endpoint name
    caller_number:      Optional[str]   = None   # raw CLI / phone number
    interface_mode:     InterfaceMode   = InterfaceMode.PHONE

    # ── Core session ──────────────────────────────────────────────────────────
    session_id:         Optional[str]   = None   # set by base on first LLM dispatch

    # ── Audio accumulation ────────────────────────────────────────────────────
    frames_np:          np.ndarray      = field(default_factory=lambda: np.array([], dtype=np.float32))
    lookback:           np.ndarray      = field(default_factory=lambda: np.array([], dtype=np.float32))
    vad_buffer:         np.ndarray      = field(default_factory=lambda: np.array([], dtype=np.float32))
    recording:          bool            = False
    last_voice_ts:      Optional[float] = None

    # ── VAD (one instance per context — preserves LSTM state per call) ────────
    vad:                Optional[VoiceActivityDetector] = None

    # ── Flow control ──────────────────────────────────────────────────────────
    # True while the system is speaking or processing — base sets this;
    # subclass RTP/TCP loops should discard incoming audio while True.
    rx_paused:          bool            = False

    # ── Barge-in ──────────────────────────────────────────────────────────────
    interrupt_event:    asyncio.Event   = field(default_factory=asyncio.Event)

    # ── Session stack (for relay sessions — used by speaker_remote) ─────────
    session_stack:      list                        = field(default_factory=list)

    # ── Transport extras (subclass may populate) ──────────────────────────────
    friendly_name:      Optional[str]               = None   # human-readable name
    speak_task:         Optional[asyncio.Task]       = None   # in-flight TTS task
    last_int0_ts:       float                        = 0.0    # barge-in timestamp

    # ── Speaker identification ────────────────────────────────────────────────
    speaker_id:         Optional[SpeakerIdentifier] = None
    identified_speaker: Optional[str]               = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def reset_audio(self):
        self.frames_np     = np.array([], dtype=np.float32)
        self.recording     = False
        self.last_voice_ts = None
        # vad_buffer intentionally NOT reset here — partial VAD chunks are fine
        # to carry across; they belong to the next utterance.

    def add_frames(self, chunk: np.ndarray):
        if self.frames_np.size == 0:
            self.frames_np = chunk.copy()
        else:
            self.frames_np = np.concatenate((self.frames_np, chunk))

    def update_lookback(self, chunk: np.ndarray):
        """Maintain a rolling window of the last LOOKBACK_SECONDS of audio."""
        max_samples = int(LOOKBACK_SECONDS * INTERNAL_RATE)
        self.lookback = np.concatenate((self.lookback, chunk))
        if len(self.lookback) > max_samples:
            self.lookback = self.lookback[-max_samples:]

    def clear_interrupt(self):
        if self.interrupt_event.is_set():
            self.interrupt_event.clear()


# ── Base interface ────────────────────────────────────────────────────────────

class BaseVoiceInterface:
    """
    Transport-agnostic voice interface engine.

    Subclasses must implement _deliver_audio() and may override the on_*
    feedback hooks. Everything else — VAD, ASR, LLM, TTS — is handled here.
    """

    def __init__(
        self,
        core_processor,
        vad_threshold:          float = 0.5,
        vad_timeout:            float = 1.0,
        speaker_id_threshold:   float = 0.7,
        transcriber                   = None,
        piper_voice                   = None,
        whisper_model_size:     str   = 'base.en',
        piper_max_concurrent:   int   = 1,
        whisper_max_concurrent: int   = 1,
    ):
        self.core_processor = core_processor
        self.vad_threshold  = vad_threshold
        self.vad_timeout          = vad_timeout   # seconds of silence before transcription
        self.speaker_id_threshold = speaker_id_threshold

        self.close_channel_phrase = "finish conversation"

        # ── Sentence splitter ─────────────────────────────────────────────────
        self.sentence_endings = re.compile(
            r'(?:(?<=[!?])(?:\s+|$))'
            r'|(?:(?<=\.)(?!\d)(?:\s+|$))'
            r'|(?:(?<=[,;])(?:\s+|$))'
            r'|[\r\n]+'
        )
        self._markdown_strip = re.compile(r'[*#`_~]')

        # ── Piper TTS ─────────────────────────────────────────────────────────
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
        self._piper_pool: Optional[_InferencePool] = None   # initialised in run()

        self.piper_syn_config = SynthesisConfig(
            volume=1.0, length_scale=1.0,
            noise_scale=1.0, noise_w_scale=1.0,
            normalize_audio=False,
        )

        # ── Whisper ASR ───────────────────────────────────────────────────────
        if transcriber is not None:
            log.info("Using shared Whisper model")
            self._whisper_instance = transcriber
        else:
            log.info("Loading Whisper model")
            self._whisper_instance = WhisperModel(model_size_or_path=whisper_model_size)
        self._whisper_max_concurrent = whisper_max_concurrent
        self._whisper_pool: Optional[_InferencePool] = None  # initialised in run()

        # ── Speaker profiles ──────────────────────────────────────────────────
        config_dir = os.path.join(os.path.dirname(__file__), '../config')
        self._speaker_profiles = load_profiles(config_dir)
        # Pre-load encoder so it's warm before first utterance
        from core.speaker_id import _get_encoder
        _get_encoder()

        log.info("BaseVoiceInterface ready")

    def _init_pools(self):
        """
        Initialise async inference pools. Must be called inside a running event
        loop — subclass run() methods should call this before serving.
        """
        self._piper_pool   = _InferencePool(self._piper_voice_instance,  self._piper_max_concurrent)
        self._whisper_pool = _InferencePool(self._whisper_instance, self._whisper_max_concurrent)

    def make_context(self, **kwargs) -> VoiceContext:
        """
        Create a fresh VoiceContext with a per-call VAD instance and
        SpeakerIdentifier. Subclasses call this at the start of each call.

        Keyword args are forwarded to VoiceContext (e.g. endpoint_id,
        caller_number, interface_mode).
        """
        ctx = VoiceContext(
            vad        = VoiceActivityDetector(threshold=self.vad_threshold),
            speaker_id = SpeakerIdentifier(self._speaker_profiles, threshold=self.speaker_id_threshold),
            **kwargs,
        )
        return ctx

    # ── Feedback hooks ────────────────────────────────────────────────────────
    # All no-ops in the base. Subclasses override what they need.

    async def on_vad_triggered(self, ctx: VoiceContext)      -> None: pass
    async def on_vad_silence_timeout(self, ctx: VoiceContext)-> None: pass
    async def on_thinking(self, ctx: VoiceContext)           -> None: pass
    async def on_speaking_start(self, ctx: VoiceContext)     -> None: pass
    async def on_speaking_end(self, ctx: VoiceContext)       -> None: pass
    async def on_session_open(self, ctx: VoiceContext)       -> None: pass
    async def on_session_close(self, ctx: VoiceContext)      -> None: pass

    # ── Audio delivery (subclass must implement) ──────────────────────────────

    async def _deliver_audio(self, ctx: VoiceContext, audio_f32: np.ndarray, sample_rate: int) -> None:
        """
        Deliver synthesised audio to the endpoint.

        audio_f32:   normalised float32 audio at sample_rate Hz.
        sample_rate: the sample rate Piper produced (may differ from the
                     transport's wire rate — subclass resamples if needed).

        This is the only method subclasses MUST implement.
        """
        raise NotImplementedError

    # ── TTS ───────────────────────────────────────────────────────────────────

    async def _speak_text(self, ctx: VoiceContext, text: str) -> None:
        """
        Synthesise text via Piper and deliver via _deliver_audio().

        Strips markdown before synthesis. Holds rx_paused for the duration
        so the transport's audio input loop discards incoming audio while
        speaking (half-duplex). Respects ctx.interrupt_event for barge-in.
        """
        text = self._markdown_strip.sub('', text).strip()
        if not text:
            return

        prev_rx_paused = ctx.rx_paused
        ctx.rx_paused  = True

        await self.on_speaking_start(ctx)

        try:
            async with self._piper_pool as voice:
                def synth_all_chunks():
                    return [
                        (chunk.sample_rate, chunk.audio_float_array.copy())
                        for chunk in voice.synthesize(text, syn_config=self.piper_syn_config)
                    ]
                try:
                    piper_chunks = await asyncio.to_thread(synth_all_chunks)
                except Exception:
                    log.error("Piper synthesis error", exc_info=True)
                    return

            for sr, audio_f32 in piper_chunks:
                if ctx.interrupt_event.is_set():
                    return
                audio_f32 = np.asarray(audio_f32, dtype=np.float32).reshape(-1)

                # Normalise to consistent RMS
                rms = float(np.sqrt(np.mean(audio_f32 ** 2))) if audio_f32.size else 0.0
                if rms > 1e-8:
                    audio_f32 *= (0.2 / rms)
                audio_f32 = np.clip(audio_f32 * 1.2, -1.0, 1.0)

                await self._deliver_audio(ctx, audio_f32, sr)
                if ctx.interrupt_event.is_set():
                    return

        finally:
            ctx.rx_paused = prev_rx_paused
            await self.on_speaking_end(ctx)

    # ── LLM dispatch ─────────────────────────────────────────────────────────

    async def _contact_core(
        self,
        ctx:          VoiceContext,
        input_text:   str,
        silent_start: bool = False,
    ) -> bool:
        """
        Dispatch input_text to the LLM, stream TTS as sentences arrive.

        silent_start=True skips the "Working" acknowledgement and thinking
        signal — used for server-initiated calls where the announcement is
        the opening turn and there is no user waiting for feedback.

        Returns True if the session was closed (hangup requested by tool).
        """
        if ctx.session_id is None:
            ctx.session_id  = str(uuid.uuid4())
            core_session    = self.core_processor.create_session(ctx.session_id)
            if core_session is not None:
                self._configure_session(ctx, core_session)

            if not silent_start:
                await self._speak_text(ctx, "Working")
                await self.on_thinking(ctx)

        log.info("Sending to core", extra={'data': f"{ctx.endpoint_id} {input_text!r}"})

        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input_text, "session_id": ctx.session_id},
            daemon=True,
        )
        thread.start()

        core_session = self.core_processor.get_session(ctx.session_id)
        if core_session is None:
            # Session was closed externally (e.g. hangup during processing)
            return True

        buffer       = ""
        ctx.rx_paused = True

        while True:
            chunk = await asyncio.to_thread(get_response_queue(core_session).get)
            if chunk is None:
                break
            buffer += chunk
            sentences = self.sentence_endings.split(buffer)
            for sent in sentences[:-1]:
                sent = sent.strip()
                if sent and not ctx.interrupt_event.is_set():
                    await self._speak_text(ctx, sent)
            buffer = sentences[-1]

        if ctx.interrupt_event.is_set():
            buffer = ""
        if buffer.strip():
            await self._speak_text(ctx, buffer.strip())

        # Re-fetch — session may have been closed while we were speaking
        core_session = self.core_processor.get_session(ctx.session_id)
        if core_session is None:
            return True

        close = hangup_requested(core_session)
        if close:
            clear_hangup(core_session)
            await self._close_session(ctx)
            return True

        ctx.rx_paused = False
        return False

    def _configure_session(self, ctx: VoiceContext, core_session: dict) -> None:
        """
        Populate core session metadata from context. Subclasses can override
        to add transport-specific keys (e.g. caller_number for asterisk).
        """
        core_session[KEY_INTERFACE_MODE] = ctx.interface_mode
        core_session['endpoint_id']      = ctx.endpoint_id
        core_session['interface']        = ctx.interface_mode.value
        if ctx.identified_speaker:
            core_session['speaker'] = ctx.identified_speaker

    async def _close_session(self, ctx: VoiceContext) -> None:
        """
        Close the core session and fire on_session_close.
        Does NOT close any transport connection — that's the subclass's job.
        """
        if ctx.session_id:
            self.core_processor.close_session(ctx.session_id)
            ctx.session_id = None
        ctx.rx_paused = False
        await self.on_session_close(ctx)

    # ── Transcription ─────────────────────────────────────────────────────────

    async def _transcribe_buffer(self, ctx: VoiceContext) -> None:
        """
        Transcribe accumulated audio, run speaker ID, dispatch to LLM.

        Takes a snapshot of ctx.frames_np before clearing, so debug WAV
        saving and Whisper operate on the same consistent buffer.
        """
        if ctx.frames_np.size == 0:
            ctx.rx_paused = False
            return

        min_samples = int(0.2 * INTERNAL_RATE)
        if ctx.frames_np.size < min_samples:
            log.debug("Ignoring short buffer",
                      extra={'data': f"{ctx.frames_np.size} samples < {min_samples}"})
            ctx.reset_audio()
            ctx.rx_paused = False
            return

        # Snapshot before clearing so nothing races with incoming audio
        audio_snapshot = ctx.frames_np.copy()
        ctx.reset_audio()
        ctx.rx_paused = True

        # Debug WAV
        if self.core_processor.config.debug.record_audio:
            self._save_debug_wav(ctx, audio_snapshot)

        # Transcribe
        try:
            async with self._whisper_pool as whisper:
                def do_transcribe():
                    return whisper.transcribe(
                        audio_snapshot,
                        language                  = 'en',
                        no_speech_threshold       = None,
                        log_prob_threshold        = None,
                        compression_ratio_threshold = None,
                    )
                segments, _ = await asyncio.to_thread(do_transcribe)
        except Exception:
            log.error("ASR error", exc_info=True)
            ctx.rx_paused = False
            return

        if not segments:
            ctx.rx_paused = False
            return

        text = " ".join(seg.text for seg in segments).strip()
        log.info("Transcription", extra={'data': f"{ctx.endpoint_id} {text!r}"})

        if not text or re.fullmatch(r'[\s.…]+', text):
            log.debug("Ignoring Whisper hallucination")
            ctx.rx_paused = False
            return

        if self.close_channel_phrase in text.lower():
            await self._close_session(ctx)
            return

        await self.on_thinking(ctx)

        # Collect speaker ID result (started at voice onset)
        ctx.identified_speaker = ctx.speaker_id.result(timeout=1.0)
        if ctx.identified_speaker:
            log.info("Speaker identified",
                     extra={'data': f"{ctx.endpoint_id} {ctx.identified_speaker!r}"})

        await self._contact_core(ctx, text)

    def _save_debug_wav(self, ctx: VoiceContext, audio: np.ndarray) -> None:
        cfg        = self.core_processor.config.debug
        record_dir = cfg.record_dir
        os.makedirs(record_dir, exist_ok=True)
        tag      = ctx.endpoint_id or 'unknown'
        filename = os.path.join(
            record_dir,
            f"voice_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav",
        )
        try:
            with wave.open(filename, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(INTERNAL_RATE)
                wf.writeframes((audio * 32767).astype(np.int16).tobytes())
            log.debug("Saved debug audio", extra={'data': filename})
        except Exception:
            log.error("Failed to save debug WAV", exc_info=True)

    # ── Audio input pipeline ──────────────────────────────────────────────────

    async def force_transcribe(self, ctx: VoiceContext) -> None:
        """
        Force immediate transcription without waiting for the silence timeout.
        Called by subclasses on an explicit end-of-utterance signal
        (e.g. STOP frame in speaker_remote).
        """
        if ctx.recording and ctx.frames_np.size > 0:
            ctx.recording     = False
            ctx.last_voice_ts = None
            await self._transcribe_buffer(ctx)

    async def _process_audio_chunk(self, ctx: VoiceContext, chunk_f32: np.ndarray) -> None:
        """
        Feed a decoded float32 chunk (at INTERNAL_RATE) into the pipeline.

        Handles VAD pre-buffering, lookback, accumulation, silence timeout,
        and transcription trigger. Subclasses call this from their audio
        receive loop after any necessary codec/rate conversion.

        While ctx.rx_paused is True (system speaking or processing) the
        subclass should still call this — it discards audio cleanly and
        prevents stale frames from accumulating.
        """
        # Discard incoming audio while speaking/processing, but keep the
        # buffers clean so we don't dump stale audio on the next utterance.
        if ctx.rx_paused:
            ctx.vad_buffer = np.array([], dtype=np.float32)
            ctx.reset_audio()
            return

        # Pre-buffer to VAD_MIN_SAMPLES before feeding Silero
        ctx.vad_buffer = np.concatenate((ctx.vad_buffer, chunk_f32))
        if len(ctx.vad_buffer) < VAD_MIN_SAMPLES:
            return

        vad_chunk      = ctx.vad_buffer[:VAD_MIN_SAMPLES]
        ctx.vad_buffer = ctx.vad_buffer[VAD_MIN_SAMPLES:]

        voice_detected = ctx.vad(audio_frame=vad_chunk)

        if voice_detected:
            if not ctx.recording:
                ctx.recording = True
                ctx.clear_interrupt()

                # Prepend lookback so we don't lose the onset of the utterance
                if ctx.lookback.size > 0:
                    ctx.add_frames(ctx.lookback)

                # Start speaker ID as soon as voice begins
                ctx.speaker_id.start(
                    get_frames   = lambda: ctx.frames_np,
                    is_recording = lambda: ctx.recording,
                )
                await self.on_vad_triggered(ctx)

            ctx.last_voice_ts = time.monotonic()
            ctx.add_frames(vad_chunk)

        elif ctx.recording:
            # Continue accumulating during brief silences — this preserves
            # mid-sentence pauses rather than fragmenting utterances.
            ctx.add_frames(vad_chunk)

            silence_s = (
                (time.monotonic() - ctx.last_voice_ts)
                if ctx.last_voice_ts is not None else 0.0
            )
            if silence_s > self.vad_timeout:
                await self.on_vad_silence_timeout(ctx)
                ctx.recording     = False
                ctx.last_voice_ts = None
                await self._transcribe_buffer(ctx)

        # Always update lookback (even during silence) so we have context
        # ready for the next utterance onset.
        ctx.update_lookback(vad_chunk)