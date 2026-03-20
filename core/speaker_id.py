"""
Speaker identification module for Supernova.

Uses resemblyzer (GE2E model) to extract speaker embeddings and identify
known speakers from short audio clips in real time.

Usage in voice interfaces:
    from core.speaker_id import SpeakerIdentifier, load_profiles

    # Initialise once at startup
    self._speaker_profiles = load_profiles(config_dir)
    self._speaker_id = SpeakerIdentifier(self._speaker_profiles)

    # When VAD fires and recording starts:
    self._speaker_id.start(
        get_frames=lambda: self.frames_np,
        is_recording=lambda: self.recording,
    )

    # Before sending to core — result is usually already ready:
    speaker = self._speaker_id.result(timeout=1.0)
    session['speaker'] = speaker

Enrollment:
    python3 scripts/enroll_speaker.py --name Jesse --audio samples/jesse.wav

Requirements:
    pip install resemblyzer soundfile librosa
"""

import os
import json
import time
import threading
import numpy as np

# Lazy-loaded — only imported on first use so startup stays fast
_encoder = None
_encoder_lock = threading.Lock()

SAMPLE_RATE = 16000  # resemblyzer works at 16kHz


def _get_encoder():
    """
    Load the resemblyzer VoiceEncoder on first use.
    Kept as a module-level singleton so it's only loaded once.
    Runs on CUDA if available, CPU otherwise.
    """
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
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
    Extract a speaker embedding from a float32 numpy array at 16kHz.
    Returns a 256-dim numpy array or None if the encoder is unavailable.
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
    """Compute cosine similarity between two numpy embedding vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── Profile storage ───────────────────────────────────────────────────────────

def load_profiles(config_dir: str) -> dict:
    """
    Load enrolled speaker embeddings from config/speaker_profiles.json.
    Returns dict of {name: {'embedding': ndarray, 'email': str, 'notes': str}}
    or empty dict if file doesn't exist.
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
                'embedding': np.array(data['embedding'], dtype=np.float32),
                'email':     data.get('email', ''),
                'notes':     data.get('notes', ''),
            }
        print(f"[speaker_id] Loaded {len(profiles)} speaker profile(s): {list(profiles.keys())}")
        return profiles
    except Exception as e:
        print(f"[speaker_id] Error loading profiles: {e}")
        return {}


def save_profile(config_dir: str, name: str, audio_np: np.ndarray,
                 email: str = '', notes: str = '') -> bool:
    """
    Enroll a new speaker by extracting their embedding and saving to profiles.
    audio_np should be at least 3 seconds of clean speech, float32 at 16kHz.
    """
    print(f"[speaker_id] Enrolling speaker: {name}")
    embedding = get_embedding(audio_np)
    if embedding is None:
        print(f"[speaker_id] Failed to extract embedding for {name}")
        return False

    path = os.path.join(config_dir, "speaker_profiles.json")
    try:
        raw = {}
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)

        raw[name] = {
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
    Compare audio against enrolled speaker profiles using cosine similarity.

    Returns the name of the best matching speaker if similarity is above
    threshold, or None if no match is found.

    threshold: 0.75 is a reasonable starting point.
               Raise if getting false positives (wrongly identifying someone).
               Lower if missing known speakers (failing to identify correctly).
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
    accumulates. Starts as soon as VAD fires so identification is usually
    complete before the utterance ends.

    Usage:
        identifier = SpeakerIdentifier(profiles)

        # When VAD fires:
        identifier.start(
            get_frames=lambda: self.frames_np,
            is_recording=lambda: self.recording,
        )

        # When VAD ends — get result (usually already ready):
        speaker = identifier.result(timeout=1.0)
    """

    def __init__(self, profiles: dict, sample_rate: int = 16000, threshold: float = 0.75):
        self.profiles    = profiles
        self.sample_rate = sample_rate
        self.threshold   = threshold
        self._result     = None
        self._done       = threading.Event()
        self._thread     = None

    def start(self, get_frames, is_recording):
        """
        Begin progressive identification in a background thread.

        get_frames:   callable returning the current frames numpy array
        is_recording: callable returning True while VAD is active
        """
        if not self.profiles:
            self._result = None
            self._done.set()
            return
        
        # Don't start a new thread if one is already running
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
        Return the identified speaker name or None if unidentified.
        Blocks for up to timeout seconds if identification is still running
        (it usually finishes well before this).
        """
        self._done.wait(timeout=timeout)
        return self._result

    def _run(self, get_frames, is_recording):
        """
        Progressive identification loop:
          1. First attempt at 1.5s of audio
          2. Retry every 0.5s with more audio until match found or recording ends
          3. Final attempt on the complete utterance if still unidentified
        """
        initial_window = int(1.5 * self.sample_rate)  # 1.5s for first attempt
        retry_window   = int(0.5 * self.sample_rate)  # add 0.5s more per retry
        poll_interval  = 0.05                          # check frames every 50ms
        max_duration   = 10.0                          # give up after 10s
        deadline       = time.monotonic() + max_duration
        target_samples = initial_window
        attempt        = 0

        while time.monotonic() < deadline:
            frames          = get_frames()
            still_recording = is_recording()

            # Wait until we have enough audio, or recording has ended
            if frames.size < target_samples and still_recording:
                time.sleep(poll_interval)
                continue

            # Snapshot the audio for this attempt
            audio_snapshot = (
                frames[:target_samples].copy()
                if frames.size >= target_samples
                else frames.copy()
            )

            # Too short for reliable ID — wait for more unless recording ended
            if audio_snapshot.size < int(0.5 * self.sample_rate):
                if not still_recording:
                    break
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
                return

            print(
                f"[speaker_id] Attempt {attempt} unidentified "
                f"({audio_snapshot.size / self.sample_rate:.1f}s), "
                f"{'retrying with more audio...' if still_recording else 'recording ended.'}"
            )

            # No more audio coming — give up
            if not still_recording:
                break

            # Expand window for next attempt
            target_samples += retry_window

        print(f"[speaker_id] Could not identify speaker after {attempt} attempt(s)")
        self._result = None
        self._done.set()