"""
core/precontext.py — Personality file loader for Supernova.

Loads agent mode personality files from the personality/ directory.
Hot-reloads on file change so edits take effect without restart.

Each agent mode has its own .md file defined in config/modes.yaml:
    general       → personality/agent_general.md
    deep_research → personality/agent_deep_research.md
    document      → personality/agent_document.md
    transcription → personality/agent_transcription.md

Usage:
    from core.precontext import PrecontextLoader

    loader = PrecontextLoader(config_dir)
    text   = loader.get('general')           # by name string
    text   = loader.get(some_agent_mode)     # by AgentMode instance
"""

import os
from core.logger import get_logger

log = get_logger('precontext')


class PrecontextLoader:
    """
    Loads and caches agent mode personality files.

    Caches file content keyed by resolved file path. On each call to get(),
    the file's mtime is checked — if it has changed the file is reloaded.
    This means personality edits take effect on the next request with no
    restart required.

    One PrecontextLoader is created at startup and shared across all sessions.
    Thread-safe for reads (dict reads are atomic in CPython).
    """

    def __init__(self, config_dir: str):
        # config_dir is kept for reference but personality files are resolved
        # relative to the project root (one level up from config/).
        self._config_dir   = config_dir
        self._project_root = os.path.dirname(config_dir)
        self._cache: dict[str, tuple[str, float]] = {}
        # path → (content, mtime)

    def get(self, agent_mode) -> str:
        """
        Return the personality text for the given agent mode.

        Accepts either an AgentMode instance or a plain string mode name.
        Falls back to an empty string if the file is missing or unreadable —
        the system still works, just without personality context.

        Usage:
            text = loader.get('general')
            text = loader.get(mode_registry.default())
        """
        # Resolve file path from agent mode
        path = self._resolve_path(agent_mode)
        if path is None:
            return ""

        return self._load(path)

    def _resolve_path(self, agent_mode) -> str | None:
        """
        Resolve the absolute path to the personality file for agent_mode.
        Returns None if the mode has no valid precontext path.
        """
        from core.agent_mode import AgentMode

        if isinstance(agent_mode, AgentMode):
            precontext = agent_mode.precontext
        elif isinstance(agent_mode, str):
            # Plain string — construct the conventional filename
            precontext = f"personality/agent_{agent_mode}.md"
        else:
            log.warning("Unknown agent_mode type", extra={'data': str(type(agent_mode))})
            return None

        if not precontext:
            return None

        # Resolve relative to project root
        if os.path.isabs(precontext):
            return precontext
        return os.path.join(self._project_root, precontext)

    def _load(self, path: str) -> str:
        """
        Load file at path, using cache if mtime hasn't changed.
        Returns empty string on any error.
        """
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            if path in self._cache:
                log.warning("Personality file deleted", extra={'data': path})
                del self._cache[path]
            else:
                log.warning("Personality file not found", extra={'data': path})
            return ""
        except Exception as e:
            log.error("Error checking personality file", extra={'data': f"{path}: {e}"})
            return ""

        # Return cached version if file hasn't changed
        cached = self._cache.get(path)
        if cached is not None:
            content, cached_mtime = cached
            if mtime == cached_mtime:
                return content

        # File changed or not yet cached — reload
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            self._cache[path] = (content, mtime)
            log.info("Personality file loaded", extra={'data': path})
            return content
        except Exception as e:
            log.error("Error loading personality file", extra={'data': f"{path}: {e}"})
            return self._cache.get(path, ("", 0))[0]   # return stale cache on error