"""
core/tool_base.py — Shared helpers for Supernova tools.

Import this at the top of any tool file to get logging, scheduling,
result formatting, and config validation — without needing to know the
internal module paths or calling conventions.

Typical tool usage:

    from core.tool_base import ToolBase

    log = ToolBase.logger('my_tool')

    def execute(tool_args, session, core, tool_config):
        # Validate required config fields up front
        err = ToolBase.require_config(tool_config, 'api_key', 'url')
        if err:
            return ToolBase.error(core, 'my_tool', err)

        params = ToolBase.params(tool_args)

        # Log
        log.info("Running my_tool", extra={'data': str(params)})

        # Immediate spoken/typed feedback before LLM responds
        ToolBase.speak(core, session, "Doing that now.")

        # Schedule a future callback
        ToolBase.schedule(core, session, tool_config,
            label         = "my reminder",
            delay_seconds = 300,
            announcement  = "Your reminder is ready.",
        )

        # Return result to LLM
        return ToolBase.result(core, 'my_tool', {
            "status":       "done",
            "instructions": "Tell the user it's done.",
        })

        # Or return an error
        return ToolBase.error(core, 'my_tool', "Something went wrong.")

────────────────────────────────────────────────────────────────────────────────
YAML CONFIG — automatically loaded, no code needed.

Each tool's config is read from config/{tool_filename}.yaml and passed
directly into execute() and provide_context() as tool_config. Nothing
needs to be imported or loaded manually.

Example: tools/my_tool.py → config/my_tool.yaml

    enabled:          true    # false to disable without deleting the file
    interfaces:       []      # [] = all, ['speaker','phone','general'] to restrict
    requires_config:  ""      # skip tool if AppConfig lacks this attribute
    context_priority: 50      # lower = earlier in system prompt (default 50)

    # Any extra fields are yours — passed to execute() and provide_context()
    api_key:          ""
    default_endpoint: ""

────────────────────────────────────────────────────────────────────────────────
INTERFACE MODE FLAGS — set in the tool's yaml sidecar:

    # Available on all interfaces (default)
    interfaces: []

    # Satellite voice + phone only
    interfaces: [speaker, phone]

    # Phone calls only
    interfaces: [phone]

    # Text interfaces only
    interfaces: [general]

────────────────────────────────────────────────────────────────────────────────
CONTEXT INJECTION — add provide_context to inject into the system prompt:

    def provide_context(core, tool_config, session):
        return "[MY TOOL]\\nRelevant context here."

────────────────────────────────────────────────────────────────────────────────
"""

import os
import logging
from core.logger import get_logger
from core.session_state import (
    get_interface_mode, get_agent_mode,
    request_hangup, get_history, set_history, clear_history,
    get_speaker, get_endpoint_id,
    KEY_INTERFACE_MODE, KEY_AGENT_MODE,
)


class ToolBase:
    """
    Static helper namespace for tool authors.
    No instantiation needed — call everything as ToolBase.method().
    """

    # ── Logging ───────────────────────────────────────────────────────────────

    @staticmethod
    def logger(tool_name: str) -> logging.Logger:
        """
        Get a logger for this tool, namespaced under supernova.tools.

        Usage:
            log = ToolBase.logger('my_tool')
            log.info("Running")
            log.warning("Something odd", extra={'data': "detail"})
            log.error("Failed", exc_info=True)
        """
        return get_logger(f'tools.{tool_name}')

    # ── Config validation ─────────────────────────────────────────────────────

    @staticmethod
    def require_config(tool_config: dict, *keys: str) -> str | None:
        """
        Check that required config keys are present and non-empty.
        Returns an error message string if any are missing, or None if all present.
        Call at the top of execute() before doing any real work.

        Usage:
            err = ToolBase.require_config(tool_config, 'api_key', 'url')
            if err:
                return ToolBase.error(core, 'my_tool', err)
        """
        missing = [k for k in keys if not tool_config.get(k)]
        if missing:
            return (
                f"Missing required config field(s): {', '.join(missing)}. "
                f"Check the tool's yaml file in the config folder."
            )
        return None

    # ── Parameter extraction ──────────────────────────────────────────────────

    @staticmethod
    def params(tool_args: dict) -> dict:
        """
        Extract the parameters dict from tool_args.

        The LLM passes arguments as tool_args['parameters']. This helper
        normalises access so tools don't need to remember the nesting.

        Usage:
            params = ToolBase.params(tool_args)
            name   = params.get('name', '')
        """
        return tool_args.get('parameters', {})

    # ── Result formatting ─────────────────────────────────────────────────────

    @staticmethod
    def result(core, tool_name: str, payload: dict) -> str:
        """
        Wrap a successful result in the standard tool envelope.

        The LLM receives this as the tool response and uses it to formulate
        its reply. Include an 'instructions' key to guide the LLM's response:

        Usage:
            return ToolBase.result(core, 'my_tool', {
                "status":       "done",
                "value":        42,
                "instructions": "Tell the user the answer is 42.",
            })
        """
        return core._wrap_tool_result(tool_name, payload)

    @staticmethod
    def error(core, tool_name: str, message: str) -> str:
        """
        Return a standardised error result.
        The LLM will read the message and relay it to the user naturally.

        Usage:
            return ToolBase.error(core, 'my_tool', "No data available right now.")
        """
        return core._wrap_tool_result(tool_name, {
            "status":  "error",
            "message": message,
        })

    # ── Immediate spoken/typed feedback ──────────────────────────────────────

    @staticmethod
    def speak(core, session: dict, text: str):
        """
        Push text to the response queue immediately — heard/seen by the user
        before the LLM generates its response.

        Use for confirmations that don't need LLM wording:
            ToolBase.speak(core, session, "Setting your timer now.")

        The LLM still runs afterward. To suppress the follow-up, include
        'instruction: Acknowledge briefly.' in your result payload.
        """
        core.send_whole_response(text, session)

    # ── Scheduling ────────────────────────────────────────────────────────────

    @staticmethod
    def schedule(
        core,
        session:       dict,
        tool_config:   dict,
        label:         str,
        delay_seconds: float,
        announcement:  str,
        event_type:    str = 'reminder',
        endpoint_id:   str = None,
        interface:     str = None,
    ) -> str:
        """
        Schedule a future callback to the user's current endpoint.

        Automatically picks up endpoint_id and interface from the session,
        with fallback to tool_config['default_endpoint'].

        Returns the event_id which can be passed to cancel_schedule().

        Usage:
            event_id = ToolBase.schedule(
                core, session, tool_config,
                label         = "pasta",
                delay_seconds = 600,
                announcement  = "The pasta timer has finished.",
                event_type    = 'timer',
            )
        """
        resolved_endpoint  = endpoint_id or session.get('endpoint_id', '') or tool_config.get('default_endpoint', '')
        resolved_interface = interface   or get_interface_mode(session).value

        return core.schedule_event(
            event_type    = event_type,
            label         = label,
            delay_seconds = delay_seconds,
            endpoint_id   = resolved_endpoint,
            announcement  = announcement,
            extra         = {
                'callback_type': resolved_interface,
            },
        )

    @staticmethod
    def cancel_schedule(core, event_id: str) -> bool:
        """
        Cancel a previously scheduled event by its id.
        Returns True if it existed and was cancelled, False if not found.

        Usage:
            cancelled = ToolBase.cancel_schedule(core, event_id)
        """
        return core.cancel_event(event_id)

    @staticmethod
    def list_scheduled(core, event_type: str = None) -> list:
        """
        List pending scheduled events, optionally filtered by type.

        Usage:
            timers = ToolBase.list_scheduled(core, event_type='timer')
            all    = ToolBase.list_scheduled(core)
        """
        return core.list_events(event_type=event_type)

    # ── Session helpers ───────────────────────────────────────────────────────

    @staticmethod
    def speaker(session: dict) -> str | None:
        """
        Return the identified speaker name for this session, or None.

        Usage:
            name = ToolBase.speaker(session)
            if name:
                log.info(f"Speaking with {name}")
        """
        return get_speaker(session)

    @staticmethod
    def interface(session: dict) -> str:
        """
        Return the interface type for this session.
        One of: 'speaker', 'phone', 'general', 'headless'.

        Usage:
            if ToolBase.interface(session) == 'general':
                # format response for text
        """
        return get_interface_mode(session).value

    @staticmethod
    def endpoint(session: dict) -> str:
        """
        Return the endpoint_id for this session — who to call back.

        Usage:
            ep = ToolBase.endpoint(session)
        """
        return session.get('endpoint_id', '')

    # ── Session boundary methods ──────────────────────────────────────────────
    # Tools must never access the session dict directly. Use these methods
    # as the boundary so session internals can change without breaking tools.

    @staticmethod
    def request_hangup(session: dict):
        """
        Signal the voice interface to close the channel after this response.
        Only meaningful for speaker and phone interfaces — safe to call on any.

        Usage:
            ToolBase.request_hangup(session)
        """
        request_hangup(session)

    @staticmethod
    def get_history(session: dict) -> list:
        """Return the conversation history for this session."""
        return get_history(session)

    @staticmethod
    def set_history(session: dict, history: list):
        """Replace the conversation history for this session."""
        set_history(session, history)

    @staticmethod
    def clear_history(session: dict):
        """Clear the conversation history for this session."""
        clear_history(session)

    @staticmethod
    def get_agent_mode(session: dict):
        """Return the current AgentMode for this session."""
        return get_agent_mode(session)

    @staticmethod
    def set_agent_mode(session: dict, mode):
        """
        Switch the session to a different agent mode.
        mode can be an AgentMode instance or a mode name string.

        Usage:
            ToolBase.set_agent_mode(session, 'deep_research')
        """
        session[KEY_AGENT_MODE] = mode

    # ── File storage ──────────────────────────────────────────────────────────
    # All tool data lives under data/{tool_name}/ at the project root.
    # Use data_path() as the foundation — JSON/text helpers are convenience
    # wrappers on top. For anything else (SQLite, CSV, binary) use data_path()
    # directly with standard Python file I/O.

    @staticmethod
    def data_path(tool_name: str, filename: str = None) -> str:
        """
        Return the path to a tool's data directory, or a file within it.
        The directory is created automatically if it doesn't exist.

        All tool data lives under data/{tool_name}/ at the project root.
        Use this as the foundation for any file I/O — never hardcode paths.

        Usage:
            # Get the directory
            dir_path = ToolBase.data_path('recipes')

            # Get a specific file path
            file_path = ToolBase.data_path('recipes', 'goulash.md')

            # Use with standard Python file I/O
            with open(ToolBase.data_path('my_tool', 'store.db')) as f:
                ...
        """
        import os as _os
        project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        dir_path     = _os.path.join(project_root, 'data', tool_name)
        _os.makedirs(dir_path, exist_ok=True)
        if filename:
            return _os.path.join(dir_path, filename)
        return dir_path

    @staticmethod
    def read_json(tool_name: str, filename: str, default=None):
        """
        Read a JSON file from the tool's data directory.
        Returns default if the file doesn't exist or can't be parsed.

        Usage:
            state = ToolBase.read_json('my_tool', 'state.json', default={})
        """
        import json as _json
        path = ToolBase.data_path(tool_name, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return _json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:
            get_logger(f'tools.{tool_name}').error(
                "Failed to read JSON", extra={'data': f"{filename}: {e}"}
            )
            return default

    @staticmethod
    def write_json(tool_name: str, filename: str, data) -> bool:
        """
        Atomically write data as JSON to the tool's data directory.
        Uses a .tmp rename so partial writes never corrupt the file.
        Returns True on success, False on failure.

        Usage:
            ToolBase.write_json('my_tool', 'state.json', my_data)
        """
        import json as _json
        import tempfile
        import os as _os
        path = ToolBase.data_path(tool_name, filename)
        tmp  = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir    = _os.path.dirname(path),
                prefix = f".{filename}.tmp."
            )
            with _os.fdopen(fd, 'w', encoding='utf-8') as f:
                _json.dump(data, f, indent=2, ensure_ascii=False)
            _os.replace(tmp, path)
            return True
        except Exception as e:
            get_logger(f'tools.{tool_name}').error(
                "Failed to write JSON", extra={'data': f"{filename}: {e}"}
            )
            try:
                if tmp and _os.path.exists(tmp):
                    _os.remove(tmp)
            except Exception:
                pass
            return False

    @staticmethod
    def read_text(tool_name: str, filename: str, default: str = "") -> str:
        """
        Read a text or markdown file from the tool's data directory.
        Returns default if the file doesn't exist.

        Usage:
            content = ToolBase.read_text('recipes', 'goulash.md')
        """
        path = ToolBase.data_path(tool_name, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return default
        except Exception as e:
            get_logger(f'tools.{tool_name}').error(
                "Failed to read file", extra={'data': f"{filename}: {e}"}
            )
            return default

    @staticmethod
    def write_text(tool_name: str, filename: str, content: str) -> bool:
        """
        Atomically write text or markdown to the tool's data directory.
        Returns True on success, False on failure.

        Usage:
            ToolBase.write_text('recipes', 'goulash.md', markdown_content)
        """
        import tempfile
        import os as _os
        path = ToolBase.data_path(tool_name, filename)
        tmp  = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir    = _os.path.dirname(path),
                prefix = f".{filename}.tmp."
            )
            with _os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            _os.replace(tmp, path)
            return True
        except Exception as e:
            get_logger(f'tools.{tool_name}').error(
                "Failed to write file", extra={'data': f"{filename}: {e}"}
            )
            try:
                if tmp and _os.path.exists(tmp):
                    _os.remove(tmp)
            except Exception:
                pass
            return False

    @staticmethod
    def list_files(tool_name: str, extension: str = None) -> list:
        """
        List filenames in the tool's data directory.
        Optionally filter by extension (e.g. '.md', '.json').

        Usage:
            all_files    = ToolBase.list_files('recipes')
            recipe_files = ToolBase.list_files('recipes', extension='.md')
        """
        dir_path = ToolBase.data_path(tool_name)
        try:
            files = os.listdir(dir_path)
            if extension:
                files = [f for f in files if f.endswith(extension)]
            return sorted(files)
        except Exception:
            return []