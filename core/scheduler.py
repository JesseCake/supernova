"""
core/scheduler.py — Fires scheduled events when their due time arrives.

The Scheduler sits on top of EventStore. It owns one background thread that
sleeps until the next event is due, fires it, removes it from the store, then
sleeps until the next one. New events wake the thread early via a threading.Event.

Usage:
    scheduler = Scheduler(event_store, fire_callback)
    scheduler.start()

    # Schedule something (usually called by a tool via core.schedule_event)
    scheduler.schedule(
        event_type   = 'timer',
        label        = 'pasta',
        due_at_iso   = '2026-03-21T19:45:00+00:00',
        endpoint_id  = 'kitchen',
        announcement = 'Your pasta timer is done.',
    )

    # Cancel by id (returned from schedule())
    scheduler.cancel(event_id)

fire_callback signature:
    def fire_callback(event: dict) -> None

The callback is called from the scheduler thread. It should be non-blocking —
i.e. it should spin up its own thread or post to an async loop rather than
doing slow work inline. CoreProcessor.schedule_event() handles this correctly.
"""

import threading
import time
from datetime import timezone
from typing import Callable, Optional

from core.event_store import EventStore, _parse_iso, _utc_now, seconds_until


class Scheduler:
    """
    Background thread that fires events at their scheduled time.

    Design:
      - One thread, sleeping on a threading.Event with a calculated timeout.
      - Adding or cancelling an event pokes the event so the thread wakes and
        recalculates its next sleep duration.
      - On startup, load_pending() is called to reschedule any events that
        survived a reboot. Missed events (already past due) are fired
        immediately with a 'missed': True flag so the callback can choose
        to announce them differently ("You had a timer that went off while
        I was restarting").
    """

    def __init__(self, store: EventStore, fire_callback: Callable):
        self._store    = store
        self._callback = fire_callback
        self._wakeup   = threading.Event()   # poked when events are added/removed
        self._stop     = threading.Event()
        self._thread   = threading.Thread(
            target=self._run, daemon=True, name="scheduler"
        )

    def start(self):
        """Start the scheduler thread and reschedule any persisted events."""
        self._thread.start()
        self._restore_persisted()

    def stop(self):
        """Signal the scheduler thread to exit."""
        self._stop.set()
        self._wakeup.set()

    def schedule(
        self,
        event_type:   str,
        label:        str,
        due_at_iso:   str,
        endpoint_id:  str,
        announcement: str,
        extra:        dict = None,
    ) -> str:
        """
        Persist and schedule a new event. Returns the event id.

        due_at_iso must be a UTC ISO 8601 string (e.g. from
        core.schedule_event which calculates it from a duration).
        """
        event = {
            'type':         event_type,
            'label':        label,
            'due_at':       due_at_iso,
            'endpoint_id':  endpoint_id,
            'announcement': announcement,
        }
        if extra:
            event.update(extra)

        event_id = self._store.add(event)
        # Wake the scheduler thread so it recalculates its next sleep.
        self._wakeup.set()
        return event_id

    def cancel(self, event_id: str) -> bool:
        """Cancel a scheduled event. Returns True if it existed."""
        result = self._store.remove(event_id)
        self._wakeup.set()
        return result

    def list_type(self, event_type: str) -> list:
        """Return all pending events of a given type."""
        return self._store.by_type(event_type)

    # ── Background thread ─────────────────────────────────────────────────────

    def _restore_persisted(self):
        """
        On startup, reload events that survived a reboot.
        Pending events are rescheduled. Missed events are fired immediately
        with a 'missed' flag so the callback can handle them appropriately.
        """
        pending, missed = self._store.load_pending()

        for event in missed:
            print(f"[scheduler] missed event: {event.get('id')} "
                  f"label={event.get('label')!r} due={event.get('due_at')}")
            event['missed'] = True
            # Fire in a thread so startup isn't blocked
            threading.Thread(
                target=self._fire, args=(event,), daemon=True
            ).start()

        if pending:
            print(f"[scheduler] restored {len(pending)} pending event(s)")
            self._wakeup.set()   # wake the thread to pick them up

    def _run(self):
        """
        Main scheduler loop.

        Finds the next due event, sleeps until it fires, fires it, repeat.
        Wakes early if _wakeup is set (new event added or event cancelled).
        """
        while not self._stop.is_set():
            self._wakeup.clear()

            # Find the event due soonest
            next_event   = None
            next_seconds = None

            for event in self._store.all():
                secs = seconds_until(event.get('due_at', ''))
                if next_seconds is None or secs < next_seconds:
                    next_seconds = secs
                    next_event   = event

            if next_event is None:
                # Nothing scheduled — sleep indefinitely until poked
                self._wakeup.wait()
                continue

            if next_seconds <= 0:
                # Due now (or overdue from a very recent add)
                self._fire(next_event)
                continue

            # Sleep until the next event (or until poked by an add/cancel)
            print(f"[scheduler] next event in {next_seconds:.0f}s: "
                  f"{next_event.get('label')!r} ({next_event.get('id')})")
            self._wakeup.wait(timeout=next_seconds)

    def _fire(self, event: dict):
        """
        Remove the event from the store and invoke the callback.
        Called from the scheduler thread (or a startup thread for missed events).
        """
        event_id = event.get('id')
        if event_id:
            self._store.remove(event_id)

        print(f"[scheduler] firing: {event_id} label={event.get('label')!r} "
              f"{'(missed)' if event.get('missed') else ''}")
        try:
            self._callback(event)
        except Exception as e:
            print(f"[scheduler] callback error for {event_id}: {e}")