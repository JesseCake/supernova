"""
core/mode_registry.py — Agent mode registry for Supernova.

Loads agent mode definitions from config/modes.yaml and provides
typed access to AgentMode instances. Hot-reloads when the yaml file
changes so new modes can be added without restarting.

Usage:
    from core.mode_registry import ModeRegistry

    registry = ModeRegistry(config_dir)

    # Get a mode by name
    mode = registry.get('deep_research')   # → AgentMode | None

    # Get the default mode
    mode = registry.default()              # → AgentMode

    # List all modes
    modes = registry.all()                 # → list[AgentMode]
    names = registry.names()               # → list[str]

    # Check if a mode exists
    if registry.exists('transcription'):
        ...
"""

import os
import time
import yaml
from typing import Optional

from core.agent_mode import AgentMode
from core.logger import get_logger

log = get_logger('mode_registry')

MODES_FILENAME = 'modes.yaml'


class ModeRegistry:
    """
    Loads and caches AgentMode instances from config/modes.yaml.

    Hot-reloads on file change — mtime is checked on every access so
    edits to modes.yaml take effect on the next request without restart.

    One ModeRegistry is created at startup and shared across all sessions.
    All methods are safe to call from multiple threads (reads are lock-free
    since Python dict reads are atomic; the reload check uses a simple
    mtime comparison).
    """

    def __init__(self, config_dir: str):
        self._path    = os.path.join(config_dir, MODES_FILENAME)
        self._modes:  dict[str, AgentMode] = {}
        self._default: Optional[AgentMode] = None
        self._mtime:  float = 0.0
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[AgentMode]:
        """
        Return an AgentMode by name, or None if not found.
        Hot-reloads if modes.yaml has changed.

        Usage:
            mode = registry.get('deep_research')
            if mode is None:
                # unknown mode name
        """
        self._reload_if_changed()
        return self._modes.get(name.lower().strip())

    def default(self) -> AgentMode:
        """
        Return the default AgentMode (is_default: true in modes.yaml).
        Falls back to the first defined mode if none is marked default.
        Falls back to a minimal built-in general mode if yaml is empty.

        Usage:
            mode = registry.default()
        """
        self._reload_if_changed()
        if self._default is not None:
            return self._default
        # Fallback — should never happen if modes.yaml is well-formed
        return self._builtin_general()

    def all(self) -> list[AgentMode]:
        """
        Return all registered AgentMode instances in definition order.

        Usage:
            for mode in registry.all():
                print(mode.name, mode.description)
        """
        self._reload_if_changed()
        return list(self._modes.values())

    def names(self) -> list[str]:
        """
        Return all registered mode names.

        Usage:
            names = registry.names()
            # → ['general', 'deep_research', 'document_analysis']
        """
        self._reload_if_changed()
        return list(self._modes.keys())

    def exists(self, name: str) -> bool:
        """
        Return True if a mode with the given name exists.

        Usage:
            if registry.exists('transcription'):
                ...
        """
        self._reload_if_changed()
        return name.lower().strip() in self._modes

    def coerce(self, name: str) -> AgentMode:
        """
        Return the named AgentMode, falling back to default if not found.
        Safe equivalent of get() that always returns something.

        Usage:
            mode = registry.coerce('unknown')   # → default mode
        """
        mode = self.get(name)
        if mode is None:
            log.warning("Unknown agent mode, falling back to default",
                        extra={'data': f"name={name!r}"})
            return self.default()
        return mode

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reload_if_changed(self) -> None:
        """Check mtime and reload if the yaml file has changed."""
        try:
            mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            self._load()

    def _load(self) -> None:
        """Load modes from yaml. Replaces the current mode registry."""
        if not os.path.exists(self._path):
            log.warning("modes.yaml not found, using built-in general mode",
                        extra={'data': self._path})
            self._modes   = {'general': self._builtin_general()}
            self._default = self._modes['general']
            return

        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}

            modes   = {}
            default = None

            for name, entry in data.items():
                if not isinstance(entry, dict):
                    log.warning("Skipping invalid mode entry",
                                extra={'data': f"name={name!r}"})
                    continue
                mode = AgentMode.from_dict(name, entry)
                modes[name] = mode
                if mode.is_default:
                    if default is not None:
                        log.warning("Multiple default modes defined, using first",
                                    extra={'data': f"keeping={default.name!r} ignoring={name!r}"})
                    else:
                        default = mode

            if not modes:
                log.warning("No modes defined in modes.yaml, using built-in general mode")
                modes   = {'general': self._builtin_general()}
                default = modes['general']

            if default is None and modes:
                # No is_default: true set — use first defined mode
                default = next(iter(modes.values()))
                log.warning("No default mode set in modes.yaml, using first",
                            extra={'data': f"mode={default.name!r}"})

            self._modes   = modes
            self._default = default
            self._mtime   = os.path.getmtime(self._path)

            log.info("Agent modes loaded",
                     extra={'data': f"{len(modes)} modes — default={default.name!r}"})

        except Exception as e:
            log.error("Failed to load modes.yaml",
                      extra={'data': str(e)})
            if not self._modes:
                # First load failure — set up fallback so the app can start
                self._modes   = {'general': self._builtin_general()}
                self._default = self._modes['general']

    @staticmethod
    def _builtin_general() -> AgentMode:
        """
        Minimal built-in fallback mode used when modes.yaml is missing
        or empty. Ensures the app always has at least one valid mode.
        """
        return AgentMode(
            name           = 'general',
            description    = 'Default conversational mode',
            precontext     = 'personality/agent_general.md',
            max_tool_loops = 5,
            is_default     = True,
            trigger_phrases= '',
        )