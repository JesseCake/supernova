"""
core/session_state.py — Typed session access helpers for Supernova core.

These helpers are for use within core/ only (core.py, tool_loader.py,
interfaces). Tools interact with session exclusively through ToolBase —
never through these helpers directly.

The session dict is a plain dict for simplicity and compatibility, but
all access to it from core/ should go through these typed helpers to:
  - Catch type errors early
  - Keep session key names in one place
  - Make session structure self-documenting
  - Provide a clear audit point for session mutations

Session shape:
    {
        # Core state
        'conversation_history':  list[dict],    # {role, content} message dicts
        'response_queue':        queue.Queue,    # str | None chunks for TTS/output
        'response_finished':     threading.Event,
        'close_voice_channel':   threading.Event,
        'cancel_event':          threading.Event,
        'ollama_stream':         object | None,  # live stream reference
        '_ts_start':             float,          # perf_counter at session creation

        # Mode
        'interface_mode':        InterfaceMode,  # set by interface, never changes
        'agent_mode':            AgentMode,      # can change mid-session

        # Identity
        'speaker':               str | None,     # identified speaker name
        'endpoint_id':           str,            # who to call back
        'interface':             str,            # interface name string (legacy)

        # Text interface callbacks
        'immediate_send':        callable | None,
        'immediate_send_only':   bool,
    }
"""

from __future__ import annotations
import threading
import queue
from typing import TYPE_CHECKING

from core.interface_mode import InterfaceMode

if TYPE_CHECKING:
    from core.agent_mode import AgentMode


# ── Keys ──────────────────────────────────────────────────────────────────────
# Single source of truth for session dict key names.
# Use these constants rather than string literals throughout core/.

KEY_HISTORY          = 'conversation_history'
KEY_RESPONSE_QUEUE   = 'response_queue'
KEY_RESPONSE_DONE    = 'response_finished'
KEY_CLOSE_CHANNEL    = 'close_voice_channel'
KEY_CANCEL           = 'cancel_event'
KEY_OLLAMA_STREAM    = 'ollama_stream'
KEY_TS_START         = '_ts_start'
KEY_INTERFACE_MODE   = 'interface_mode'
KEY_AGENT_MODE       = 'agent_mode'
KEY_SPEAKER          = 'speaker'
KEY_ENDPOINT_ID      = 'endpoint_id'
KEY_IMMEDIATE_SEND   = 'immediate_send'
KEY_IMMEDIATE_ONLY   = 'immediate_send_only'
KEY_SESSION_ID        = 'session_id'


# ── Interface mode ─────────────────────────────────────────────────────────────

def get_interface_mode(session: dict) -> InterfaceMode:
    """
    Return the InterfaceMode for this session.
    Falls back to GENERAL if not set or invalid.
    """
    value = session.get(KEY_INTERFACE_MODE)
    if isinstance(value, InterfaceMode):
        return value
    if isinstance(value, str):
        return InterfaceMode.coerce(value)
    return InterfaceMode.GENERAL


def set_interface_mode(session: dict, mode: InterfaceMode) -> None:
    """
    Set the interface mode for this session.
    Should only be called once at session creation by the interface.
    """
    if not isinstance(mode, InterfaceMode):
        raise TypeError(f"Expected InterfaceMode, got {type(mode).__name__}")
    session[KEY_INTERFACE_MODE] = mode


# ── Agent mode ────────────────────────────────────────────────────────────────

def get_agent_mode(session: dict) -> 'AgentMode | None':
    """
    Return the current AgentMode for this session, or None if not set.
    None means the default mode should be used — resolved by ModeRegistry.
    """
    return session.get(KEY_AGENT_MODE)


def set_agent_mode(session: dict, mode: 'AgentMode') -> None:
    """
    Set the agent mode for this session.
    Can be called mid-session to switch working modes.
    """
    from core.agent_mode import AgentMode
    if not isinstance(mode, AgentMode):
        raise TypeError(f"Expected AgentMode, got {type(mode).__name__}")
    session[KEY_AGENT_MODE] = mode


# ── Conversation history ───────────────────────────────────────────────────────

def get_history(session: dict) -> list:
    """Return the conversation history list. Never returns None."""
    history = session.get(KEY_HISTORY)
    if not isinstance(history, list):
        return []
    return history


def set_history(session: dict, history: list) -> None:
    """Replace the conversation history entirely."""
    if not isinstance(history, list):
        raise TypeError(f"Expected list, got {type(history).__name__}")
    session[KEY_HISTORY] = history


def append_history(session: dict, message: dict) -> None:
    """Append a single message dict to the conversation history."""
    if not isinstance(message, dict):
        raise TypeError(f"Expected dict, got {type(message).__name__}")
    history = get_history(session)
    history.append(message)
    session[KEY_HISTORY] = history


def clear_history(session: dict) -> None:
    """Wipe the conversation history."""
    session[KEY_HISTORY] = []


# ── Identity ──────────────────────────────────────────────────────────────────

def get_speaker(session: dict) -> str | None:
    """Return the identified speaker name, or None if unknown."""
    return session.get(KEY_SPEAKER)


def get_endpoint_id(session: dict) -> str:
    """Return the endpoint_id for this session."""
    return session.get(KEY_ENDPOINT_ID, '')

def get_session_id(session: dict) -> str:
    """Return the session ID for this session."""
    return session.get(KEY_SESSION_ID, 'unknown')


# ── Voice channel control ─────────────────────────────────────────────────────

def request_hangup(session: dict) -> None:
    """Signal that the voice channel should close after this response."""
    event = session.get(KEY_CLOSE_CHANNEL)
    if isinstance(event, threading.Event):
        event.set()


def clear_hangup(session: dict) -> None:
    """Clear the hangup signal — called at the start of each turn."""
    event = session.get(KEY_CLOSE_CHANNEL)
    if isinstance(event, threading.Event):
        event.clear()


def hangup_requested(session: dict) -> bool:
    """True if a tool has requested the voice channel close."""
    event = session.get(KEY_CLOSE_CHANNEL)
    if isinstance(event, threading.Event):
        return event.is_set()
    return False


# ── Response queue ────────────────────────────────────────────────────────────

def get_response_queue(session: dict) -> queue.Queue:
    """Return the response queue for this session."""
    return session[KEY_RESPONSE_QUEUE]


def get_immediate_send(session: dict):
    """Return the immediate_send callback, or None if not set."""
    return session.get(KEY_IMMEDIATE_SEND)


def is_immediate_send_only(session: dict) -> bool:
    """True if all output should go via immediate_send rather than the queue."""
    return bool(session.get(KEY_IMMEDIATE_ONLY, False))


# ── Cancellation ──────────────────────────────────────────────────────────────

def get_cancel_event(session: dict) -> threading.Event | None:
    """Return the cancellation event for this session."""
    return session.get(KEY_CANCEL)


def is_cancelled(session: dict) -> bool:
    """True if the current response has been cancelled (barge-in)."""
    event = session.get(KEY_CANCEL)
    if isinstance(event, threading.Event):
        return event.is_set()
    return False


# ── Timing ────────────────────────────────────────────────────────────────────

def get_ts_start(session: dict) -> float | None:
    """Return the session start timestamp (perf_counter), or None."""
    return session.get(KEY_TS_START)