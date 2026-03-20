#!/usr/bin/env python3
"""
Simple voice recording utility for speaker enrollment (if using this machine).

Records audio from the default microphone and saves it as a WAV file.

Usage:
    python3 scripts/record_sample.py --out samples/jesse.wav
    python3 scripts/record_sample.py --out samples/jesse.wav --duration 8
    python3 scripts/record_sample.py --list    # list available devices
    python3 scripts/record_sample.py --out samples/jesse.wav --device 2

Requirements:
    pip install pyaudio
    # or on Ubuntu/Jetson:
    # sudo apt-get install python3-pyaudio
"""

import sys
import os
import wave
import argparse
import struct

SAMPLE_RATE = 16000
CHANNELS    = 1
FORMAT      = None   # set after pyaudio import (pyaudio.paInt16)
CHUNK       = 1024


def list_devices(pa):
    print(f"Available audio input devices:")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info['maxInputChannels'] > 0:
            default = " ← default" if i == pa.get_default_input_device_info()['index'] else ""
            print(f"  [{i}] {info['name']}{default}")


def record(out_path: str, duration: int, device: int | None = None):
    import pyaudio

    pa     = pyaudio.PyAudio()
    format = pyaudio.paInt16

    device_info = pa.get_device_info_by_index(device) if device is not None else pa.get_default_input_device_info()
    print(f"Recording device: {device_info['name']}")
    print(f"Duration:         {duration}s")
    print(f"Output:           {out_path}")
    print()
    print("Press Enter to start recording...")
    input()

    stream = pa.open(
        format=format,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=device,
        frames_per_buffer=CHUNK,
    )

    frames      = []
    total_chunks = int(SAMPLE_RATE / CHUNK * duration)

    print("● Recording — speak now!")
    for i in range(total_chunks):
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)

        # Simple progress bar
        progress = int((i + 1) / total_chunks * 30)
        bar      = "█" * progress + "░" * (30 - progress)
        elapsed  = (i + 1) * CHUNK / SAMPLE_RATE
        print(f"\r  [{bar}] {elapsed:.1f}s / {duration}s", end='', flush=True)

    print(f"\n■ Done.")

    stream.stop_stream()
    stream.close()
    pa.terminate()

    # Save as WAV
    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else '.', exist_ok=True)
    with wave.open(out_path, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b''.join(frames))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\n✓ Saved to {out_path} ({size_kb:.0f} KB)")
    print(f"\nEnroll with:")
    print(f"  python3 scripts/enroll_speaker.py --name <name> --audio {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record a voice sample for speaker enrollment")
    parser.add_argument('--out',      metavar='FILE',       help="Output WAV file path (e.g. samples/jesse.wav)")
    parser.add_argument('--duration', type=int, default=8,  help="Recording duration in seconds (default: 8)")
    parser.add_argument('--device',   type=int,             help="Input device index (default: system default)")
    parser.add_argument('--list',     action='store_true',  help="List available input devices")
    args = parser.parse_args()

    try:
        import pyaudio
    except ImportError:
        print("pyaudio not installed.")
        print("Install with:  pip install pyaudio")
        print("           or: sudo apt-get install python3-pyaudio")
        sys.exit(1)

    pa = pyaudio.PyAudio()

    if args.list:
        list_devices(pa)
        pa.terminate()
        sys.exit(0)

    if not args.out:
        parser.print_help()
        sys.exit(1)

    pa.terminate()
    record(args.out, args.duration, args.device)