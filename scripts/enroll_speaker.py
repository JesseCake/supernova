#!/usr/bin/env python3
"""
Speaker enrollment script for Supernova.

Records or loads audio samples and saves speaker embeddings to
config/speaker_profiles.json for use by the real-time speaker identifier.

Usage:
    # Enroll from an existing audio file (WAV, recommended 5-10s of clean speech):
    python3 scripts/enroll_speaker.py --name Jesse --audio samples/jesse.wav

    # Enroll with email and notes (used by tools for personalisation):
    python3 scripts/enroll_speaker.py --name Jesse --audio samples/jesse.wav \\
        --email jesse@example.com --notes "Primary developer. Prefers brief responses."

    # Record directly from microphone (requires sounddevice):
    python3 scripts/enroll_speaker.py --name Dean --record --duration 8

    # List enrolled speakers:
    python3 scripts/enroll_speaker.py --list

    # Remove a speaker:
    python3 scripts/enroll_speaker.py --remove Jesse

    # Test identification against an audio file:
    python3 scripts/enroll_speaker.py --test --audio samples/test.wav

    # Test with a different threshold:
    python3 scripts/enroll_speaker.py --test --audio samples/test.wav --threshold 0.80

    # Update email or notes without re-recording:
    python3 scripts/enroll_speaker.py --update Jesse --email new@example.com
    python3 scripts/enroll_speaker.py --update Jesse --notes "Prefers brief responses."
    python3 scripts/enroll_speaker.py --update Jesse --email new@example.com --notes "Updated notes."

    # Merge new audio into an existing profile (combines with previous enrollment):
    python3 scripts/enroll_speaker.py --merge Jesse --audio samples/jesse_phone.wav
    python3 scripts/enroll_speaker.py --merge Jesse --audio samples/jesse_phone.wav samples/jesse_phone2.wav

Tips for good enrollment:
    - Use 5-10 seconds of clean speech with no background noise
    - Speak naturally — the same way you would to Supernova
    - Avoid music, TV, or other voices in the background
    - Record in the same room/acoustic environment you will use Supernova in
    - Re-enroll if identification accuracy is poor
    - For phone use, try enrolling with audio recorded over the phone if possible
"""

import sys
import os
import argparse
import json
import numpy as np

# Add project root to path so we can import core modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

CONFIG_DIR    = os.path.join(os.path.dirname(__file__), '../config')
PROFILES_PATH = os.path.join(CONFIG_DIR, 'speaker_profiles.json')
SAMPLE_RATE   = 16000


def load_audio_file(path: str) -> np.ndarray:
    """Load an audio file and resample to 16kHz mono float32."""
    try:
        import soundfile as sf
        import librosa
        audio, sr = sf.read(path, dtype='float32')
        # Convert to mono if stereo
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # Resample to 16kHz if needed
        if sr != SAMPLE_RATE:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
        duration = len(audio) / SAMPLE_RATE
        print(f"Loaded {path} — {duration:.1f}s at {SAMPLE_RATE}Hz")
        return audio.astype(np.float32)
    except ImportError:
        print("soundfile or librosa not installed.")
        print("Install with: pip install soundfile librosa")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading audio file: {e}")
        sys.exit(1)


def record_audio(duration: int) -> np.ndarray:
    """Record audio from the default microphone."""
    try:
        import sounddevice as sd
        print(f"Recording {duration}s — speak now!")
        audio = sd.rec(
            int(duration * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype='float32',
        )
        sd.wait()
        print("Recording complete.")
        return audio.squeeze()
    except ImportError:
        print("sounddevice not installed: pip install sounddevice")
        sys.exit(1)
    except Exception as e:
        print(f"Recording error: {e}")
        sys.exit(1)


def cmd_enroll(args):
    """Enroll a new speaker from an audio file or microphone recording."""
    from core.speaker_id import save_profile, _get_encoder

    print("Loading voice encoder (first run downloads ~17MB model)...")
    if _get_encoder() is None:
        print("Failed to load encoder. Install with: pip install resemblyzer")
        sys.exit(1)

    if args.audio:
        if len(args.audio) == 1:
            audio_np = load_audio_file(args.audio[0])
        else:
            # Multiple files — concatenate them all into one recording
            import soundfile as sf
            import librosa
            print(f"Combining {len(args.audio)} audio files...")
            parts = []
            for path in args.audio:
                part = load_audio_file(path)
                parts.append(part)
            audio_np = np.concatenate(parts)
            duration = len(audio_np) / SAMPLE_RATE
            print(f"Combined duration: {duration:.1f}s")
    elif args.record:
        audio_np = record_audio(args.duration)
    else:
        print("Provide --audio <file> or --record")
        sys.exit(1)

    duration = len(audio_np) / SAMPLE_RATE
    if duration < 3.0:
        print(f"Warning: audio is only {duration:.1f}s — recommend at least 5s for reliable enrollment")

    success = save_profile(
        config_dir=CONFIG_DIR,
        name=args.name,
        audio_np=audio_np,
        email=args.email or '',
        notes=args.notes or '',
    )

    if success:
        print(f"\n✓ Enrolled '{args.name}' successfully.")
        print(f"  Email: {args.email or '(not set)'}")
        print(f"  Notes: {args.notes or '(not set)'}")
        print(f"\nRun --list to see all enrolled speakers.")
        print(f"Run --test --audio <file> to verify identification works.")
    else:
        print(f"\n✗ Enrollment failed.")
        sys.exit(1)


def cmd_update(args):
    """Update email and/or notes for an enrolled speaker without re-recording."""
    if not os.path.exists(PROFILES_PATH):
        print("No speaker profiles found.")
        sys.exit(1)

    with open(PROFILES_PATH) as f:
        raw = json.load(f)

    if args.name not in raw:
        print(f"Speaker '{args.name}' not found.")
        print(f"Enrolled speakers: {list(raw.keys())}")
        sys.exit(1)

    if not args.email and not args.notes:
        print("Provide at least one of --email or --notes to update.")
        sys.exit(1)

    if args.email is not None:
        raw[args.name]['email'] = args.email
        print(f"  Email → {args.email}")

    if args.notes is not None:
        raw[args.name]['notes'] = args.notes
        print(f"  Notes → {args.notes}")

    with open(PROFILES_PATH, 'w') as f:
        json.dump(raw, f, indent=2)

    print(f"\n✓ Updated '{args.name}' successfully.")

def cmd_merge(args):
    """Merge new audio into an existing speaker profile, combining with previous enrollment."""
    from core.speaker_id import get_embedding, _get_encoder

    print("Loading voice encoder...")
    if _get_encoder() is None:
        print("Failed to load encoder.")
        sys.exit(1)

    if not args.audio:
        print("Provide at least one --audio file to merge")
        sys.exit(1)

    if not os.path.exists(PROFILES_PATH):
        print(f"No profiles found — use --name to do a fresh enrollment instead.")
        sys.exit(1)

    with open(PROFILES_PATH) as f:
        raw = json.load(f)

    if args.name not in raw:
        print(f"Speaker '{args.name}' not found.")
        print(f"Enrolled speakers: {list(raw.keys())}")
        sys.exit(1)

    # Start with the existing embedding
    embeddings = [np.array(raw[args.name]['embedding'], dtype=np.float32)]
    print(f"Loaded existing embedding for '{args.name}'")

    # Extract embedding from each new audio file
    for path in args.audio:
        print(f"Processing {path}...")
        audio_np = load_audio_file(path)
        duration = len(audio_np) / SAMPLE_RATE
        if duration < 2.0:
            print(f"  Warning: only {duration:.1f}s — may be unreliable")
        emb = get_embedding(audio_np)
        if emb is not None:
            embeddings.append(emb)
            print(f"  ✓ Extracted embedding from {duration:.1f}s of audio")
        else:
            print(f"  ✗ Failed to extract embedding from {path}")

    if len(embeddings) < 2:
        print("No new embeddings extracted — aborting")
        sys.exit(1)

    # Average all embeddings and re-normalise
    merged = np.mean(embeddings, axis=0)
    merged = merged / np.linalg.norm(merged)

    raw[args.name]['embedding'] = merged.tolist()
    if args.email:
        raw[args.name]['email'] = args.email
    if args.notes:
        raw[args.name]['notes'] = args.notes

    with open(PROFILES_PATH, 'w') as f:
        json.dump(raw, f, indent=2)

    print(f"\n✓ Merged {len(embeddings)} embedding(s) into '{args.name}' profile.")
    print(f"Run --test --audio <file> to verify the updated profile works.")

def cmd_list(args):
    """List all enrolled speakers and their details."""
    if not os.path.exists(PROFILES_PATH):
        print("No speaker profiles found.")
        print(f"Enroll with: python3 scripts/enroll_speaker.py --name <name> --audio <file>")
        return

    with open(PROFILES_PATH) as f:
        raw = json.load(f)

    if not raw:
        print("No speaker profiles enrolled.")
        return

    print(f"Enrolled speakers ({len(raw)}):")
    for name, data in raw.items():
        email = data.get('email') or '(no email)'
        notes = data.get('notes') or '(no notes)'
        dims  = len(data.get('embedding', []))
        print(f"\n  {name}")
        print(f"    Email:     {email}")
        print(f"    Notes:     {notes}")
        print(f"    Embedding: {dims} dimensions")


def cmd_remove(args):
    """Remove an enrolled speaker by name."""
    if not os.path.exists(PROFILES_PATH):
        print("No speaker profiles found.")
        return

    with open(PROFILES_PATH) as f:
        raw = json.load(f)

    if args.name not in raw:
        print(f"Speaker '{args.name}' not found.")
        print(f"Enrolled speakers: {list(raw.keys())}")
        sys.exit(1)

    del raw[args.name]
    with open(PROFILES_PATH, 'w') as f:
        json.dump(raw, f, indent=2)

    print(f"✓ Removed '{args.name}' from speaker profiles.")


def cmd_test(args):
    """Test speaker identification against an audio file or recording."""
    from core.speaker_id import load_profiles, identify, _get_encoder

    print("Loading voice encoder...")
    if _get_encoder() is None:
        print("Failed to load encoder.")
        sys.exit(1)

    profiles = load_profiles(CONFIG_DIR)
    if not profiles:
        print("No profiles to test against. Enroll some speakers first.")
        sys.exit(1)

    if args.audio:
        audio_np = load_audio_file(args.audio[0])
    elif args.record:
        audio_np = record_audio(args.duration)
    else:
        print("Provide --audio <file> or --record")
        sys.exit(1)

    threshold = args.threshold or 0.75
    print(f"\nTesting against {len(profiles)} profile(s) (threshold={threshold})...")

    speaker = identify(audio_np, profiles, threshold=threshold)

    if speaker:
        profile = profiles[speaker]
        print(f"\n✓ Identified as: {speaker}")
        if profile.get('email'):
            print(f"  Email: {profile['email']}")
        if profile.get('notes'):
            print(f"  Notes: {profile['notes']}")
    else:
        print(f"\n✗ Could not identify speaker")
        print(f"  Try lowering --threshold (current: {threshold})")
        print(f"  Or re-enroll with more/better quality audio")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Supernova speaker enrollment and testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument('--list',      action='store_true',  help="List enrolled speakers")
    parser.add_argument('--remove',    metavar='NAME',        help="Remove an enrolled speaker")
    parser.add_argument('--update',    metavar='NAME',        help="Update email/notes for an enrolled speaker")
    parser.add_argument('--merge',     metavar='NAME',        help="Merge audio into an existing speaker profile")
    parser.add_argument('--name',      metavar='NAME',        help="Speaker name for enrollment")
    parser.add_argument('--audio',     metavar='FILE', nargs='+', help="Audio file(s) to use (WAV recommended)")
    parser.add_argument('--record',    action='store_true',  help="Record from microphone")
    parser.add_argument('--duration',  type=int, default=8,  help="Recording duration in seconds (default: 8)")
    parser.add_argument('--email',     metavar='EMAIL',       help="Speaker email address")
    parser.add_argument('--notes',     metavar='TEXT',        help="Notes about this speaker")
    parser.add_argument('--test',      action='store_true',  help="Test identification against audio")
    parser.add_argument('--threshold', type=float,            help="Similarity threshold for --test (default: 0.75)")

    args = parser.parse_args()

    if args.list:
        cmd_list(args)
    elif args.remove:
        args.name = args.remove
        cmd_remove(args)
    elif args.update:
        args.name = args.update
        cmd_update(args)
    elif args.merge:
        args.name = args.merge
        cmd_merge(args)
    elif args.test:
        cmd_test(args)
    elif args.name:
        cmd_enroll(args)
    else:
        parser.print_help()