"""
core/presence_registry.py — User presence and contact resolution for Supernova.

Presence model per interface:
  telegram  — always reachable if chat_id is configured. No session tracking.
  speaker   — endpoint always connectable if satellite is registered.
              WHO is there = last voice-identified user, expires after TTL.
  email     — always reachable if address is configured.

Contact detail sources (read from existing config files, not duplicated here):
  telegram  → config/telegram_interface.yaml  (endpoints block)
  email     → config/send_email.yaml          (contacts block)
  speaker   → runtime last-seen from voice ID

Unavailability marks:
  Set when a contact attempt fails (silence timeout, wrong-person rejection).
  Clears after TTL so the interface can be retried later.

Usage (from speaker_remote_interface):
    core.presence_registry.set_last_seen('jesse', 'supernova-voice',
        confidence='voice_confirmed')

Usage (from tools):
    result = core.presence_registry.get_best_contact('dean')
    result = core.presence_registry.get_best_contact('dean', preferred='telegram')
    if result:
        interface, details = result

    user_id = core.presence_registry.find_user_by_contact('telegram', 'chat_id', '123')
"""

import os
import time
import yaml
import threading
from typing import Optional
from core.logger import get_logger

log = get_logger('presence_registry')

# How long a speaker last-seen record is considered valid
SPEAKER_PRESENCE_TTL = 4 * 3600   # 4 hours

# Normalise user-facing interface names to internal values
INTERFACE_ALIASES = {
    'telegram':  'telegram',
    'im':        'telegram',
    'message':   'telegram',
    'text':      'telegram',
    'chat':      'telegram',
    'speaker':   'speaker',
    'voice':     'speaker',
    'satellite': 'speaker',
    'call':      'phone',
    'phone':     'phone',
    'ring':      'phone',
    'email':     'email',
    'mail':      'email',
}


class PresenceRegistry:
    """
    Resolves how to contact known users across interfaces.

    Availability per interface:
      - telegram/email: available if configured in their respective yaml files
      - speaker: available if endpoint is registered AND last-seen within TTL

    One instance lives on CoreProcessor. Thread-safe.
    """

    def __init__(self, config_dir: str):
        self._config_dir = config_dir
        self._lock       = threading.Lock()

        # From user_profiles.yaml — priority and retry policy
        self._profiles: dict  = {}
        self._mtime:    float = 0.0

        # Speaker last-seen — user_id → {'endpoint_id', 'ts', 'confidence'}
        self._speaker_last_seen: dict = {}

        # Unavailability marks — user_id → interface → expiry timestamp
        self._unavailable: dict = {}

        # Cached contact details from existing config files
        self._contact_cache:       dict  = {}
        self._contact_cache_mtime: float = 0.0

        self._load_profiles()

    # ── Profile loading ───────────────────────────────────────────────────────

    def _profiles_path(self) -> str:
        return os.path.join(self._config_dir, 'user_profiles.yaml')

    def _load_profiles(self):
        path = self._profiles_path()
        if not os.path.exists(path):
            log.warning("user_profiles.yaml not found", extra={'data': path})
            return
        try:
            mtime = os.path.getmtime(path)
            if mtime == self._mtime:
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            with self._lock:
                self._profiles = data
                self._mtime    = mtime
            log.info("User profiles loaded",
                     extra={'data': f"{len(data)} users: {list(data.keys())}"})
        except Exception as e:
            log.error("Failed to load user_profiles.yaml", extra={'data': str(e)})

    def _reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self._profiles_path())
            if mtime != self._mtime:
                self._load_profiles()
        except FileNotFoundError:
            pass

    # ── Contact detail resolution from existing configs ───────────────────────

    def _load_contact_cache(self):
        """
        Read contact details from existing interface/tool config files.
        Keyed by (interface, friendly_name_lower) → contact details dict.
        Hot-reloads when config files change.
        """
        files = [
            os.path.join(self._config_dir, 'telegram_interface.yaml'),
            os.path.join(self._config_dir, 'send_email.yaml'),
        ]
        latest = max(
            (os.path.getmtime(f) for f in files if os.path.exists(f)),
            default=0.0,
        )
        if latest == self._contact_cache_mtime:
            return

        cache = {}

        # Telegram — endpoints block
        tg_path = os.path.join(self._config_dir, 'telegram_interface.yaml')
        if os.path.exists(tg_path):
            try:
                with open(tg_path, 'r', encoding='utf-8') as f:
                    tg = yaml.safe_load(f) or {}
                for name, ep in (tg.get('endpoints') or {}).items():
                    friendly = ep.get('friendly_name', name).lower()
                    chat_id  = ep.get('chat_id', '')
                    if chat_id:
                        cache[('telegram', friendly)] = {'chat_id': chat_id}
            except Exception as e:
                log.error("Failed to read telegram_interface.yaml",
                          extra={'data': str(e)})

        # Email — contacts block in send_email.yaml
        email_path = os.path.join(self._config_dir, 'send_email.yaml')
        if os.path.exists(email_path):
            try:
                with open(email_path, 'r', encoding='utf-8') as f:
                    em = yaml.safe_load(f) or {}
                for name, address in (em.get('contacts') or {}).items():
                    if address:
                        cache[('email', name.lower())] = {'address': address}
            except Exception as e:
                log.error("Failed to read send_email.yaml",
                          extra={'data': str(e)})

        with self._lock:
            self._contact_cache            = cache
            self._contact_cache_mtime      = latest

    def _get_contact_details(self, user_id: str, interface: str) -> dict | None:
        """
        Return contact details for a user on an interface.
        Returns None if not configured or not reachable.
        """
        self._load_contact_cache()
        profile = self._get_profile(user_id)
        if not profile:
            return None

        friendly = profile.get('friendly_name', user_id).lower()

        if interface == 'speaker':
            # Speaker: use last-seen presence
            presence = self.get_speaker_presence(user_id)
            if presence:
                return {
                    'endpoint_id': presence['endpoint_id'],
                    'confidence':  presence['confidence'],
                    'age_seconds': presence['age_seconds'],
                }
            return None

        # Telegram and email: look up from config cache
        with self._lock:
            return self._contact_cache.get((interface, friendly))

    # ── Speaker last-seen ─────────────────────────────────────────────────────

    def set_last_seen(
        self,
        user_id:     str,
        endpoint_id: str,
        confidence:  str = 'assumed',
    ):
        """
        Record that a user was last seen on a speaker endpoint.
        Called by speaker_remote_interface after voice identification.

        Args:
            user_id:     e.g. 'jesse'
            endpoint_id: e.g. 'supernova-voice'
            confidence:  'voice_confirmed' | 'assumed'

        Usage:
            core.presence_registry.set_last_seen('jesse', 'supernova-voice',
                confidence='voice_confirmed')
        """
        with self._lock:
            self._speaker_last_seen[user_id] = {
                'endpoint_id': endpoint_id,
                'ts':          time.monotonic(),
                'confidence':  confidence,
            }
        log.info("Speaker last seen",
                 extra={'data': f"{user_id} on {endpoint_id} ({confidence})"})

    def get_speaker_presence(self, user_id: str) -> dict | None:
        """
        Return speaker presence if within TTL, else None.
        Returns {'endpoint_id', 'confidence', 'age_seconds'}
        """
        with self._lock:
            record = self._speaker_last_seen.get(user_id)
        if record is None:
            return None
        age = time.monotonic() - record['ts']
        if age > SPEAKER_PRESENCE_TTL:
            return None
        return {
            'endpoint_id': record['endpoint_id'],
            'confidence':  record['confidence'],
            'age_seconds': age,
        }

    # ── Unavailability marking ────────────────────────────────────────────────

    def mark_unavailable(self, user_id: str, interface: str, ttl: float = 300):
        """
        Temporarily mark a contact method unavailable after a failed attempt.
        Clears after ttl seconds.

        Called on: silence timeout, wrong-person rejection, send failure.
        """
        with self._lock:
            self._unavailable.setdefault(user_id, {})[interface] = (
                time.monotonic() + ttl
            )
        log.info("Contact marked unavailable",
                 extra={'data': f"{user_id} on {interface} for {ttl}s"})

    def is_unavailable(self, user_id: str, interface: str) -> bool:
        """Return True if this contact method is currently marked unavailable."""
        with self._lock:
            expiry = self._unavailable.get(user_id, {}).get(interface)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            with self._lock:
                self._unavailable.get(user_id, {}).pop(interface, None)
            return False
        return True

    def clear_unavailable(self, user_id: str, interface: str = None):
        """Clear unavailability marks for a user (all interfaces if interface=None)."""
        with self._lock:
            if interface:
                self._unavailable.get(user_id, {}).pop(interface, None)
            else:
                self._unavailable.pop(user_id, None)

    # ── Contact resolution ────────────────────────────────────────────────────

    def get_best_contact(
        self,
        user_id:   str,
        preferred: str = None,
    ) -> tuple[str, dict] | None:
        """
        Return (interface, contact_details) for the best way to reach user_id.

        If preferred specified:
          - Returns that interface if configured and not unavailable
          - Returns None if not configured (caller should tell the user)

        Otherwise walks contact_priority, skipping unavailable interfaces.

        Returns None if no contact method available.
        """
        self._reload_if_changed()

        profile = self._get_profile(user_id)
        if profile is None:
            log.warning("Unknown user", extra={'data': user_id})
            return None

        priority = profile.get('contact_priority', [])

        if preferred:
            preferred = self.normalise_interface(preferred)
            if self.is_unavailable(user_id, preferred):
                log.info("Preferred interface marked unavailable",
                         extra={'data': f"{user_id} {preferred}"})
                return None
            details = self._get_contact_details(user_id, preferred)
            if details:
                log.info("Best contact (preferred)",
                         extra={'data': f"{user_id} via {preferred}"})
                return (preferred, details)
            log.info("Preferred interface not configured",
                     extra={'data': f"{user_id} {preferred}"})
            return None

        for interface in priority:
            if self.is_unavailable(user_id, interface):
                continue
            details = self._get_contact_details(user_id, interface)
            if details:
                log.info("Best contact",
                         extra={'data': f"{user_id} via {interface}"})
                return (interface, details)

        log.warning("No contact method available", extra={'data': user_id})
        return None

    # ── Reverse lookup ────────────────────────────────────────────────────────

    def find_user_by_contact(
        self,
        interface: str,
        key:       str,
        value:     str,
    ) -> str | None:
        """
        Find a user_id by an interface-specific identifier.
        Used by interfaces to map incoming connections to known users.

        Usage:
            user_id = registry.find_user_by_contact('telegram', 'chat_id', '987')
            user_id = registry.find_user_by_contact('speaker', 'endpoint_id', 'supernova-voice')
        """
        self._reload_if_changed()
        self._load_contact_cache()

        # Speaker — check last-seen records
        if interface == 'speaker' and key == 'endpoint_id':
            with self._lock:
                for uid, record in self._speaker_last_seen.items():
                    if record.get('endpoint_id') == value:
                        return uid

        # Other interfaces — search contact cache by value, return user_id via friendly name
        with self._lock:
            profiles = dict(self._profiles)
            cache    = dict(self._contact_cache)

        for (iface, friendly), details in cache.items():
            if iface != interface:
                continue
            if str(details.get(key, '')) == str(value):
                for uid, profile in profiles.items():
                    if profile.get('friendly_name', '').lower() == friendly:
                        return uid
        return None

    # ── User info ─────────────────────────────────────────────────────────────

    def get_friendly_name(self, user_id: str) -> str:
        """Return the friendly name for a user, or user_id if not found."""
        profile = self._get_profile(user_id)
        return profile.get('friendly_name', user_id) if profile else user_id

    def get_retry_policy(self, user_id: str) -> dict:
        """Return the retry policy for a user with sensible defaults."""
        defaults = {
            'max_attempts':        2,
            'attempt_gap_seconds': 30,
            'fallback_on_failure': True,
            'silence_timeout':     15,
            'session_timeout':     30,
        }
        profile = self._get_profile(user_id)
        if profile:
            defaults.update(profile.get('retry_policy', {}))
        return defaults

    def all_users(self) -> list[str]:
        """Return all known user IDs."""
        self._reload_if_changed()
        with self._lock:
            return list(self._profiles.keys())

    def _get_profile(self, user_id: str) -> dict | None:
        self._reload_if_changed()
        with self._lock:
            return self._profiles.get(user_id)

    # ── Interface name normalisation ──────────────────────────────────────────

    @staticmethod
    def normalise_interface(name: str) -> str:
        """
        Normalise a user-facing interface name to the internal value.
        e.g. 'IM' → 'telegram', 'voice' → 'speaker', 'mail' → 'email'
        """
        return INTERFACE_ALIASES.get(name.lower().strip(), name.lower().strip())