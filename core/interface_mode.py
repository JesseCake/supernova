"""
core/interface_mode.py — Interface mode enum for Supernova.

Defines the three interfaces a user can interact through.
Set once at session creation by the interface, never changes during a session.

Usage:
    from core.interface_mode import InterfaceMode

    # Set at session creation
    session['interface_mode'] = InterfaceMode.SPEAKER

    # Compare
    if interface_mode == InterfaceMode.SPEAKER:
        ...

    # Convert to string for system message injection
    str(InterfaceMode.SPEAKER)   # → 'speaker'
    
    # Safe coercion from string
    mode = InterfaceMode.from_str('speaker')   # → InterfaceMode.SPEAKER
    mode = InterfaceMode.coerce('bad_value')   # → InterfaceMode.GENERAL (safe fallback)
"""

from enum import Enum


class InterfaceMode(Enum):
    """
    The interface through which the user is interacting with Supernova.

    SPEAKER — satellite voice device (voice_remote). Responses should be
              short, spoken-word friendly. No markdown, no URLs, no lists. Can hang up.

    PHONE   — phone call via Asterisk. Very short responses. Can hang up.

    GENERAL — text-based interface (Telegram, web, headless). Markdown
              supported. Responses can be longer and more detailed.
    """
    SPEAKER = 'speaker'
    PHONE   = 'phone'
    GENERAL = 'general'

    def __str__(self) -> str:
        """Return the string value — useful for system message injection."""
        return self.value

    def is_voice(self) -> bool:
        """True for any voice interface (speaker or phone)."""
        return self in (InterfaceMode.SPEAKER, InterfaceMode.PHONE)

    def is_text(self) -> bool:
        """True for text-based interfaces (general)."""
        return self == InterfaceMode.GENERAL

    def can_hangup(self) -> bool:
        """True if this interface supports hanging up."""
        return self in (InterfaceMode.SPEAKER, InterfaceMode.PHONE)

    @classmethod
    def from_str(cls, value: str) -> 'InterfaceMode':
        """
        Coerce a string to an InterfaceMode.
        Raises ValueError if the value is not a valid interface mode.

        Usage:
            mode = InterfaceMode.from_str('speaker')  # → InterfaceMode.SPEAKER
            mode = InterfaceMode.from_str('bad')       # → raises ValueError
        """
        try:
            return cls(value.lower().strip())
        except ValueError:
            valid = [m.value for m in cls]
            raise ValueError(
                f"Invalid interface mode: {value!r}. Valid values: {valid}"
            )

    @classmethod
    def coerce(cls, value: str) -> 'InterfaceMode':
        """
        Like from_str but falls back to GENERAL rather than raising.
        Use when handling untrusted input (e.g. yaml config, tool args).

        Usage:
            mode = InterfaceMode.coerce('speaker')    # → InterfaceMode.SPEAKER
            mode = InterfaceMode.coerce('bad_value')  # → InterfaceMode.GENERAL
        """
        try:
            return cls.from_str(value)
        except ValueError:
            return cls.GENERAL