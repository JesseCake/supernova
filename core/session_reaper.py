# core/session_reaper.py

import threading
import time
from core.logger import get_logger

log = get_logger('session_reaper')


class SessionReaper:
    """
    Mixin for interfaces that need periodic stale session cleanup (IM, web interfaces, things that maybe just leave sessions dangling/accumulating).
    
    Usage in an interface:
        self._reaper = SessionReaper(
            get_active_sessions = lambda: self._sessions,      # dict of id → session_id
            get_last_active     = lambda: self._last_active,   # dict of id → timestamp
            close_fn            = self._close_stale_session,   # callable(id)
            ttl_seconds         = 1800,
            check_interval      = 60,
        )
        self._reaper.start()
    """

    def __init__(
        self,
        get_active_sessions,
        get_last_active,
        close_fn,
        ttl_seconds:    int = 1800,
        check_interval: int = 60,
    ):
        self._get_active  = get_active_sessions
        self._get_active_ts = get_last_active
        self._close_fn    = close_fn
        self._ttl         = ttl_seconds
        self._interval    = check_interval
        self._thread      = None

    def start(self):
        self._thread = threading.Thread(
            target  = self._loop,
            daemon  = True,
            name    = 'session-reaper',
        )
        self._thread.start()
        log.info("Session reaper started",
                 extra={'data': f"ttl={self._ttl}s interval={self._interval}s"})

    def _loop(self):
        while True:
            time.sleep(self._interval)
            try:
                self._reap()
            except Exception as e:
                log.error("Reaper error", extra={'data': str(e)})

    def _reap(self):
        now      = time.monotonic()
        sessions = dict(self._get_active())
        active   = dict(self._get_active_ts())
        stale    = []

        for identifier, last_ts in active.items():
            if (now - last_ts) > self._ttl:
                stale.append(identifier)

        for identifier in stale:
            log.info("Reaping stale session",
                     extra={'data': f"id={identifier} idle={now - active[identifier]:.0f}s"})
            try:
                self._close_fn(identifier)
            except Exception as e:
                log.error("Reaper close error",
                          extra={'data': f"id={identifier} {e}"})