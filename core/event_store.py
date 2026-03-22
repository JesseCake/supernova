"""
core/event_store.py — Persistent registry of scheduled events.

Stores events as JSON on disk so they survive reboots. Each event has enough
information to reconstruct its countdown on startup — specifically a UTC due
time rather than a remaining duration, so the remaining time can be recalculated
correctly regardless of how long the server was down.

Usage:
    store = EventStore(config_dir)

    # Add an event (returns the generated id)
    event_id = store.add({
        'type':         'timer',
        'label':        'pasta',
        'endpoint_id':  'kitchen',
        'due_at':       '2026-03-21T19:45:00',   # ISO format UTC
        'announcement': 'Your pasta timer is done.',
        'created_at':   '2026-03-21T19:40:00',
    })

    # Remove when fired or cancelled
    store.remove(event_id)

    # List all pending (due_at in future) on startup
    pending = store.load_pending()

    # List all for user queries ("what timers do I have set?")
    all_events = store.all()

File location: data/scheduler/scheduled_events.json
"""

import json
import os
import uuid
import threading
from datetime import datetime, timezone
from typing import Optional

from core.logger import get_logger
log = get_logger('event_store')


EVENTS_FILENAME = "scheduled_events.json"


class EventStore:
    """
    Thread-safe persistent store for scheduled events.

    All writes go through _save() which rewrites the whole file atomically
    (write to .tmp then rename). Reads load from the in-memory dict which
    is populated at construction and kept in sync on every write.
    """

    def __init__(self, config_dir: str):
        # Store in data/scheduler/ alongside other tool data.
        # config_dir is kept as a parameter for backwards compatibility
        # but is no longer used for the storage path.
        project_root    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir        = os.path.join(project_root, 'data', 'scheduler')
        os.makedirs(data_dir, exist_ok=True)
        self._path  = os.path.join(data_dir, EVENTS_FILENAME)
        self._lock  = threading.Lock()
        self._events: dict = {}   # id → event dict
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, event: dict) -> str:
        """
        Persist a new event. Assigns a unique id and sets created_at if missing.
        Returns the event id.

        The caller must include at minimum:
            type        str   — e.g. 'timer', 'reminder'
            due_at      str   — ISO 8601 UTC datetime string
            endpoint_id str   — which satellite to call
            announcement str  — text injected as context for the LLM call
        """
        with self._lock:
            event_id = uuid.uuid4().hex[:8]
            event    = dict(event)   # don't mutate caller's dict
            event['id']         = event_id
            event['created_at'] = event.get('created_at', _now_iso())
            self._events[event_id] = event
            self._save_locked()
            log.info("Event added", extra={'data': f"id={event_id} type={event.get('type')} label={event.get('label')!r} due={event.get('due_at')}"})
            return event_id

    def remove(self, event_id: str) -> bool:
        """Remove an event by id. Returns True if it existed."""
        with self._lock:
            if event_id not in self._events:
                return False
            label = self._events[event_id].get('label', event_id)
            del self._events[event_id]
            self._save_locked()
            log.info("Event removed", extra={'data': f"id={event_id} label={label!r}"})
            return True

    def all(self) -> list:
        """Return all events (past and future) as a list of dicts."""
        with self._lock:
            return list(self._events.values())

    def load_pending(self) -> list:
        """
        Return events whose due_at is in the future.
        Called at startup to reschedule surviving events.
        Events whose due_at has already passed are returned separately
        as 'missed' so the caller can decide whether to fire them or discard.
        """
        now    = _utc_now()
        pending = []
        missed  = []
        with self._lock:
            for event in self._events.values():
                due = _parse_iso(event.get('due_at', ''))
                if due is None:
                    continue
                if due > now:
                    pending.append(dict(event))
                else:
                    missed.append(dict(event))
        return pending, missed

    def get(self, event_id: str) -> Optional[dict]:
        """Return a single event by id, or None."""
        with self._lock:
            e = self._events.get(event_id)
            return dict(e) if e else None

    def by_type(self, event_type: str) -> list:
        """Return all events of a given type."""
        with self._lock:
            return [dict(e) for e in self._events.values()
                    if e.get('type') == event_type]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self):
        """Load events from disk. Safe to call if file doesn't exist yet."""
        if not os.path.exists(self._path):
            self._events = {}
            return
        try:
            with open(self._path, 'r') as f:
                raw = json.load(f)
            # Support both list format and dict format
            if isinstance(raw, list):
                self._events = {e['id']: e for e in raw if 'id' in e}
            elif isinstance(raw, dict):
                self._events = raw
            else:
                self._events = {}
            log.info("Events loaded", extra={'data': f"{len(self._events)} event(s) from {self._path}"})
        except Exception as e:
            log.error("Error loading events", extra={'data': f"{self._path}: {e}"})
            self._events = {}

    def _save_locked(self):
        """
        Write events to disk. Must be called with self._lock held.
        Writes to a .tmp file then renames for atomicity.
        """
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, 'w') as f:
                json.dump(list(self._events.values()), f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            log.error("Error saving events", extra={'data': f"{self._path}: {e}"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _now_iso() -> str:
    return _utc_now().isoformat()

def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO 8601 string to a timezone-aware datetime. Returns None on failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Assume UTC if no timezone specified
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def seconds_until(due_at_iso: str) -> float:
    """Return seconds until due_at_iso from now. Negative if already past."""
    due = _parse_iso(due_at_iso)
    if due is None:
        return 0.0
    return (due - _utc_now()).total_seconds()