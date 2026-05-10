"""
vad.py — Lean Silero VAD wrapper, numpy + ONNX only.

Drops the torch dependency entirely. The ONNX runtime takes numpy arrays
directly; the original whisper-live wrapper converted numpy → torch → numpy
purely as an artefact of the upstream utility code it was based on.

Public API is backwards-compatible with the original VoiceActivityDetector:

    vad = VoiceActivityDetector(threshold=0.5, frame_rate=16000)
    is_speech: bool = vad(audio_frame_float32)   # audio_frame is 1-D np.float32

The lower-level SileroVAD class is also importable if you need direct access
to the ONNX session (e.g. for batch inference or state management).
"""

import os
import subprocess
import numpy as np
import onnxruntime

_MODEL_URL  = "https://github.com/snakers4/silero-vad/raw/v4.0/files/silero_vad.onnx"
_CACHE_PATH = os.path.expanduser("~/.cache/whisper-live/silero_vad.onnx")

SUPPORTED_RATE = 16000


def _ensure_model(url: str = _MODEL_URL, path: str = _CACHE_PATH) -> str:
    """Download the ONNX model if not already cached. Returns the local path."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            subprocess.run(["wget", "-q", "-O", path, url], check=True)
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"Failed to download Silero VAD model from {url}. "
                f"Download it manually and place it at {path}."
            )
    return path


class SileroVAD:
    """
    Thin stateful wrapper around the Silero VAD ONNX model.

    Maintains the LSTM hidden/cell state (h, c) between calls so that
    the model has temporal context across consecutive audio chunks — this
    is the correct usage pattern for streaming inference.

    Call reset_states() between utterances or speakers if needed.

    Args:
        force_cpu: Always use the CPU ONNX provider even if CUDA is available.
                   VAD is lightweight enough that CPU is always preferable to
                   avoid GPU memory pressure.
    """

    def __init__(self, force_cpu: bool = True):
        path = _ensure_model()

        opts = onnxruntime.SessionOptions()
        opts.log_severity_level   = 3   # suppress ONNX runtime info/warnings
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1

        providers = (
            ['CPUExecutionProvider']
            if force_cpu or 'CPUExecutionProvider' in onnxruntime.get_available_providers()
            else ['CUDAExecutionProvider']
        )
        self.session = onnxruntime.InferenceSession(path, providers=providers, sess_options=opts)
        self.reset_states()

    def reset_states(self, batch_size: int = 1) -> None:
        """Reset LSTM state. Call this between independent audio streams."""
        self._h = np.zeros((2, batch_size, 64), dtype=np.float32)
        self._c = np.zeros((2, batch_size, 64), dtype=np.float32)

    def __call__(self, chunk: np.ndarray, sample_rate: int) -> float:
        """
        Run inference on a single audio chunk.

        Args:
            chunk:       1-D float32 numpy array of audio samples, normalised
                         to [-1.0, 1.0]. Must be at least ~32ms of audio
                         (512 samples @ 16kHz, 256 @ 8kHz).
            sample_rate: 8000 or 16000 Hz.

        Returns:
            Speech probability in [0.0, 1.0].
        """
        if sample_rate != SUPPORTED_RATE:
            raise ValueError(f"sample_rate must be {SUPPORTED_RATE}, got {sample_rate}")

        # ONNX expects shape (1, num_samples) — add batch dim without copying
        x = chunk.reshape(1, -1).astype(np.float32)

        min_samples = 512 if sample_rate == 16000 else 256
        if x.shape[1] < min_samples:
            raise ValueError(
                f"Audio chunk too short: {x.shape[1]} samples "
                f"(minimum {min_samples} @ {sample_rate} Hz)"
            )

        ort_inputs = {
            'input': x,
            'h':     self._h,
            'c':     self._c,
            'sr':    np.array(sample_rate, dtype=np.int64),
        }
        out, self._h, self._c = self.session.run(None, ort_inputs)

        # out shape: (1, 1) — scalar speech probability
        return float(out[0, 0])


class VoiceActivityDetector:
    """
    Drop-in replacement for the original whisper-live VoiceActivityDetector.

    Converts a float32 audio chunk to a bool (speech / not speech) using
    the Silero VAD ONNX model. No torch dependency.

    Args:
        threshold:  Speech probability above which a frame is considered voice.
        frame_rate: Sample rate of incoming audio (8000 or 16000 Hz).
        force_cpu:  Pin ONNX to the CPU provider (recommended — VAD is cheap).
    """

    def __init__(
        self,
        threshold:  float = 0.5,
        frame_rate: int   = 16000,
        force_cpu:  bool  = True,
    ):
        self.threshold  = threshold
        self.frame_rate = frame_rate
        self._model     = SileroVAD(force_cpu=force_cpu)

    def reset(self) -> None:
        """Reset LSTM state — call between independent audio streams."""
        self._model.reset_states()

    def __call__(self, audio_frame: np.ndarray) -> bool:
        """
        Args:
            audio_frame: 1-D float32 numpy array at self.frame_rate sample rate.

        Returns:
            True if speech probability exceeds self.threshold.
        """
        prob = self._model(audio_frame, self.frame_rate)
        return prob > self.threshold