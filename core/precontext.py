"""
Dynamic precontext loader for Supernova.

Watches personality.md and mode-specific instruction files in config/
and reloads them automatically when they change — no restart needed.

Files:
    config/personality.md          — always loaded, core personality
    config/voice_instructions.md   — appended in SPEAKER and PHONE modes
    config/phone_instructions.md   — appended in PHONE mode only
"""

import os
from enum import Enum


class VoiceMode(Enum):
    PLAIN   = "plain"    # no voice additions
    SPEAKER = "speaker"  # voice instructions + hangup tool
    PHONE   = "phone"    # phone-specific instructions


class PrecontextLoader:
    def __init__(self, config_dir: str):
        self.config_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../personality'
        )
        self._cache = {}     # filename -> content
        self._mtimes = {}    # filename -> mtime
        self._files = [
            'personality.md',
            'voice_instructions.md',
            'phone_instructions.md',
        ]

    def _load_file(self, filename: str) -> str:
        """Read a file, returning empty string if it doesn't exist."""
        path = os.path.join(self.config_dir, filename)
        try:
            mtime = os.path.getmtime(path)
            if self._mtimes.get(filename) == mtime:
                return self._cache.get(filename, "")
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            self._cache[filename] = content
            self._mtimes[filename] = mtime
            print(f"[precontext] Reloaded {filename}")
            return content
        except FileNotFoundError:
            print(f"[precontext] {filename} not found, skipping")
            self._cache[filename] = ""
            self._mtimes[filename] = 0.0
            return ""

    def get(self, mode: VoiceMode = VoiceMode.PLAIN) -> str:
        """
        Return the full precontext string for the given mode.
        Re-reads any files that have changed on disk since last call.
        """
        parts = [self._load_file('personality.md')]

        if mode in (VoiceMode.SPEAKER, VoiceMode.PHONE):
            parts.append(self._load_file('voice_instructions.md'))

        if mode == VoiceMode.PHONE:
            parts.append(self._load_file('phone_instructions.md'))

        return "\n\n".join(p for p in parts if p)