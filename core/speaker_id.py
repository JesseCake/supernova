"""
speaker_id.py — Real-time speaker identification for Supernova.
 
Uses resemblyzer (GE2E speaker encoder) to extract 256-dimensional voice
embeddings and compare them against enrolled speaker profiles using cosine
similarity.
 
Architecture:
  - A module-level singleton VoiceEncoder is lazy-loaded on first use so that
    importing this module doesn't slow down startup. _get_encoder() handles
    thread-safe initialisation.
  - SpeakerIdentifier runs identification progressively in a background thread,
    starting as soon as VAD fires (in voice_remote.py's _handle_client AUD0
    handler). By the time the utterance ends and Whisper transcription is done,
    speaker ID has usually already reached a confident result.
 
Enrollment:
    python3 scripts/enroll_speaker.py --name Jesse --audio samples/jesse.wav
 
Requirements:
    pip install resemblyzer soundfile librosa
 
Integration pattern (in VoiceRemoteInterface.__init__):
    self._speaker_profiles = load_profiles(config_dir)
    self._speaker_id       = SpeakerIdentifier(self._speaker_profiles)
    from core.speaker_id import _get_encoder
    _get_encoder()   # pre-warm: loads model at startup, not on first utterance
 
Usage (in _handle_client AUD0 handler — on VAD rising edge):
    self._speaker_id.start(
        get_frames=lambda: self.frames_np,
        is_recording=lambda: self.recording,
    )
 
Usage (in _transcribe_buffer, after THNK is sent):
    self._identified_speaker = self._speaker_id.result(timeout=1.0)
"""

import os
import json
import time
import threading
import numpy as np

# _encoder is lazy-loaded on first call to _get_encoder() so that importing
# this module does not trigger ONNX/CUDA initialisation at import time.
_encoder = None
_encoder_lock = threading.Lock()

# resemblyzer works at 16kHz — all audio must be resampled to this rate
# before passing to get_embedding() or identify().
SAMPLE_RATE = 16000  # resemblyzer works at 16kHz


def _get_encoder():
    """
    Return the module-level VoiceEncoder singleton, loading it on first call.
 
    Thread-safe: uses double-checked locking so only one thread ever runs the
    heavy model-load path. Subsequent callers get the cached instance instantly.
 
    Returns None (and prints a warning) if resemblyzer is not installed.
    CUDA is used automatically if onnxruntime-gpu is installed.
    """
    global _encoder
    if _encoder is not None:
        return _encoder  # fast path — already loaded
    
    with _encoder_lock:
        # Second check inside the lock: another thread may have loaded it
        # between our first check and acquiring the lock.
        if _encoder is not None:
            return _encoder
        try:
            from resemblyzer import VoiceEncoder
            print("[speaker_id] Loading voice encoder model...")
            _encoder = VoiceEncoder()  # auto-selects CUDA if available
            print("[speaker_id] Voice encoder loaded.")
        except ImportError:
            print("[speaker_id] WARNING: resemblyzer not installed. Speaker ID disabled.")
            print("[speaker_id] Install with: pip install resemblyzer")
            _encoder = None
    return _encoder


def get_embedding(audio_np: np.ndarray) -> np.ndarray | None:
    """
    Extract a 256-dimensional speaker embedding from float32 PCM audio at 16kHz.
 
    audio_np must be:
      - dtype float32, values in [-1.0, 1.0]
      - sample rate 16000 Hz
      - at least ~0.5 seconds long for reliable results
 
    Returns None if the encoder is unavailable (resemblyzer not installed).
    Clips audio to [-1,1] as a safety measure against over-driven input.
    """
    encoder = _get_encoder()
    if encoder is None:
        return None
    try:
        audio = np.clip(audio_np.astype(np.float32), -1.0, 1.0)
        return encoder.embed_utterance(audio)
    except Exception as e:
        print(f"[speaker_id] Embedding error: {e}")
        return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute the cosine similarity between two embedding vectors.
 
    Returns a value in [-1, 1] where 1 = identical direction, 0 = orthogonal,
    -1 = opposite direction.  For speaker embeddings, values above ~0.75 are
    typically the same speaker; below ~0.65 are different speakers.
 
    Returns 0.0 if either vector is the zero vector (degenerate case).
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── Profile storage ───────────────────────────────────────────────────────────

def load_profiles(config_dir: str) -> dict:
    """
    Load enrolled speaker embeddings from config/speaker_profiles.json.
 
    Returns a dict:
        {
            "Jesse": {
                "embedding": np.ndarray (shape [256], float32),
                "email":     "jesse@example.com",
                "notes":     "primary user",
            },
            ...
        }
 
    Returns an empty dict if the file doesn't exist or can't be parsed.
    This means speaker ID is gracefully disabled if no one has been enrolled.
 
    Note: called both at startup (in VoiceRemoteInterface.__init__) AND on each
    turn inside create_system_message() (to inject the speaker name). The
    startup call caches the profiles in _speaker_profiles; the per-turn call
    re-reads from disk so that newly enrolled speakers are picked up without
    a server restart.
    """
    path = os.path.join(config_dir, "speaker_profiles.json")
    if not os.path.exists(path):
        print(f"[speaker_id] No profiles found at {path} — speaker ID disabled")
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
        profiles = {}
        for name, data in raw.items():
            profiles[name] = {
                # Stored as a plain list in JSON; convert back to float32 ndarray.
                'embedding': np.array(data['embedding'], dtype=np.float32),
                'email':     data.get('email', ''),
                'notes':     data.get('notes', ''),
            }
        print(f"[speaker_id] Loaded {len(profiles)} speaker profile(s): {list(profiles.keys())}")
        return profiles
    except Exception as e:
        print(f"[speaker_id] Error loading profiles: {e}")
        return {}


def save_profile(config_dir: str, name: str, audio_np: np.ndarray, email: str = '', notes: str = '') -> bool:
    """
    Enroll a new speaker by extracting their embedding and persisting it.
 
    audio_np should be at least 3 seconds of clean speech at 16kHz float32.
    Longer samples (5-10s) give more stable embeddings.
 
    The profile is saved to config/speaker_profiles.json (created if absent).
    Existing profiles are preserved; the named speaker is upserted.
 
    Returns True on success, False if embedding extraction or file write fails.
    """
    print(f"[speaker_id] Enrolling speaker: {name}")
    embedding = get_embedding(audio_np)
    if embedding is None:
        print(f"[speaker_id] Failed to extract embedding for {name}")
        return False

    path = os.path.join(config_dir, "speaker_profiles.json")
    try:
        # Load existing profiles so we don't overwrite other speakers.
        raw = {}
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)

        raw[name] = {
            # tolist() converts ndarray to a plain Python list for JSON serialisation.
            'embedding': embedding.tolist(),
            'email':     email,
            'notes':     notes,
        }

        with open(path, 'w') as f:
            json.dump(raw, f, indent=2)

        print(f"[speaker_id] Saved profile for {name} to {path}")
        return True
    except Exception as e:
        print(f"[speaker_id] Error saving profile: {e}")
        return False


def identify(audio_np: np.ndarray, profiles: dict, threshold: float = 0.75) -> str | None:
    """
    Compare audio against all enrolled speaker profiles.
 
    Returns the name of the best-matching speaker if their cosine similarity
    exceeds the threshold, or None if no speaker matches.
 
    Threshold tuning:
      0.75 — good starting point for most household/office microphones.
      Raise (toward 0.85) if you're getting false positives (wrong person identified).
      Lower (toward 0.65) if known speakers aren't being recognised.
 
    Called by SpeakerIdentifier._run() at each attempt. Also usable standalone
    for batch post-processing (e.g. in the enrollment script).
    """
    if not profiles:
        return None

    embedding = get_embedding(audio_np)
    if embedding is None:
        return None

    best_name  = None
    best_score = 0.0

    for name, profile in profiles.items():
        score = cosine_similarity(embedding, profile['embedding'])
        print(f"[speaker_id] {name}: similarity={score:.3f}")
        if score > best_score:
            best_score = score
            best_name  = name

    if best_score >= threshold:
        print(f"[speaker_id] Match: {best_name} (score={best_score:.3f})")
        return best_name

    print(f"[speaker_id] No match (best={best_name} score={best_score:.3f} threshold={threshold})")
    return None


# ── Progressive real-time identifier ─────────────────────────────────────────

class SpeakerIdentifier:
    """
    Runs speaker identification progressively in a background thread as audio
    accumulates during an utterance.
 
    The key insight is that speaker embeddings are reliable after just 1.5s of
    speech, and we can start identifying as soon as VAD fires — long before
    the utterance ends and Whisper runs.  By overlapping identification with
    speech capture, we eliminate the speaker ID wait from the perceived latency.
 
    Thread safety:
      - start() may be called from the asyncio thread.
      - _run() executes in a daemon Thread.
      - result() may be called from the asyncio thread.
      - get_frames / is_recording lambdas access self.frames_np and self.recording
        from voice_remote.py. These are written by the asyncio thread and read by
        the identification thread. Because numpy array assignment is effectively
        atomic in CPython (the GIL protects it), this is safe without an explicit
        lock for this specific use case.
 
    Usage:
        identifier = SpeakerIdentifier(profiles)
 
        # On VAD rising edge (speech detected):
        identifier.start(
            get_frames=lambda: self.frames_np,
            is_recording=lambda: self.recording,
        )
 
        # After THNK is sent — result is usually already available:
        speaker = identifier.result(timeout=1.0)
    """

    def __init__(self, profiles: dict, sample_rate: int = 16000, threshold: float = 0.75):
        self.profiles    = profiles
        self.sample_rate = sample_rate
        self.threshold   = threshold
        self._result     = None  # identified speaker name, or None
        self._done       = threading.Event()  # set when identification is complete
        self._thread     = None

    def start(self, get_frames, is_recording):
        """
        Begin progressive identification in a background daemon thread.
 
        get_frames:   zero-argument callable that returns the current frames_np
                      numpy array (the live accumulation buffer).
        is_recording: zero-argument callable that returns True while VAD is active.
 
        If no profiles are loaded, marks done immediately with None result.
        If a thread is already running (previous utterance overlapped), skips
        starting a new one — the existing thread will finish naturally.
        """
        if not self.profiles:
            # No enrolled speakers — can't identify anyone.
            self._result = None
            self._done.set()
            return
        
        # Prevent overlapping threads (shouldn't happen in normal use, but guard
        # against edge cases like very fast consecutive utterances).
        if self._thread and self._thread.is_alive():
            return

        self._result = None
        self._done.clear()

        self._thread = threading.Thread(
            target=self._run,
            args=(get_frames, is_recording),
            daemon=True,
        )
        self._thread.start()

    def result(self, timeout: float = 2.0) -> str | None:
        """
        Return the identified speaker name, or None if unidentified.
 
        Blocks for up to `timeout` seconds if identification is still in progress.
        In practice, with the progressive strategy and pre-warming, identification
        is usually complete before the caller gets here.
 
        Called in _transcribe_buffer() after THNK is sent (so the satellite's
        feedback beep fires immediately) and after any residual _speak_task is
        awaited.
        """
        self._done.wait(timeout=timeout)
        return self._result

    def _run(self, get_frames, is_recording):
        """
        Progressive identification loop.
 
        Strategy:
          1. Wait for 1.5s of audio to accumulate (enough for a reliable embedding).
          2. Try to identify. If match found → done.
          3. If no match: wait for 0.5s more audio and retry.
          4. Continue until a match is found, recording ends, or 10s deadline.
          5. If recording has ended and still no match → done with None.
 
        The expanding window (retry_window) means we try with more audio on each
        attempt, which improves accuracy for speakers with soft voices or noisy
        environments where the first 1.5s might not be clean enough.
 
        poll_interval (50ms) is the sleep between "do we have enough audio yet?"
        checks — short enough to respond quickly without busy-waiting.
        """
        initial_window = int(1.5 * self.sample_rate)  # 1.5s for first attempt
        retry_window   = int(0.5 * self.sample_rate)  # add 0.5s more per retry
        poll_interval  = 0.05                          # check frames every 50ms
        max_duration   = 10.0                          # give up after 10s
        deadline       = time.monotonic() + max_duration
        target_samples = initial_window
        attempt        = 0

        while time.monotonic() < deadline:
            frames          = get_frames()  # snapshot current buffer length
            still_recording = is_recording()    # is VAD still active?

            # Wait until we have enough audio, or recording has ended
            if frames.size < target_samples and still_recording:
                time.sleep(poll_interval)
                continue

            # Snapshot the audio for this attempt — take either target_samples
            # or everything available (if recording just ended with less).
            audio_snapshot = (
                frames[:target_samples].copy()
                if frames.size >= target_samples
                else frames.copy()
            )

            # Too short for a reliable embedding — wait unless recording is done.
            if audio_snapshot.size < int(0.5 * self.sample_rate):
                if not still_recording:
                    break   # recording ended with very little audio — give up
                time.sleep(poll_interval)
                continue

            attempt += 1
            speaker  = identify(audio_snapshot, self.profiles, self.threshold)

            if speaker:
                print(
                    f"[speaker_id] Identified '{speaker}' on attempt {attempt} "
                    f"({audio_snapshot.size / self.sample_rate:.1f}s of audio)"
                )
                self._result = speaker
                self._done.set()
                return  # done — signal waiters

            print(
                f"[speaker_id] Attempt {attempt} unidentified "
                f"({audio_snapshot.size / self.sample_rate:.1f}s), "
                f"{'retrying with more audio...' if still_recording else 'recording ended.'}"
            )

            # Recording ended and we still couldn't identify — give up.
            if not still_recording:
                break

            # Expand the audio window for the next attempt.
            target_samples += retry_window

        print(f"[speaker_id] Could not identify speaker after {attempt} attempt(s)")
        self._result = None
        self._done.set()  # signal waiters even on failure so result() doesn't time out