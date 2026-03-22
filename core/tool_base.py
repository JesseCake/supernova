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
    voice_only:       false   # true = satellite voice only (VoiceMode.SPEAKER)
    phone_only:       false   # true = phone calls only (VoiceMode.PHONE)
    requires_config:  ""      # skip tool if AppConfig lacks this attribute
    context_priority: 50      # lower = earlier in system prompt (default 50)

    # Any extra fields are yours — passed to execute() and provide_context()
    api_key:          ""
    default_endpoint: ""

────────────────────────────────────────────────────────────────────────────────
INTERFACE MODE FLAGS — set in the tool's yaml sidecar:

    # Available on all interfaces (default)
    voice_only: false
    phone_only: false

    # Satellite voice only (VoiceMode.SPEAKER)
    voice_only: true

    # Phone calls only (VoiceMode.PHONE)
    phone_only: true

────────────────────────────────────────────────────────────────────────────────
CONTEXT INJECTION — add provide_context to inject into the system prompt:

    def provide_context(core, tool_config):
        return ToolBase.context_for_modes(
            core,
            text  = "[MY TOOL]\\nRelevant context here.",
            modes = ('plain', 'speaker', 'phone'),  # all modes
        )

────────────────────────────────────────────────────────────────────────────────
"""

import logging
from core.logger import get_logger


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
        resolved_interface = interface   or session.get('interface', 'voice_remote')

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
        return session.get('speaker')

    @staticmethod
    def interface(session: dict) -> str:
        """
        Return the interface type for this session.
        One of: 'voice_remote', 'asterisk', 'telegram', 'headless', or 'web'.

        Usage:
            if ToolBase.interface(session) == 'telegram':
                # format response for text
        """
        return session.get('interface', 'unknown')

    @staticmethod
    def endpoint(session: dict) -> str:
        """
        Return the endpoint_id for this session — who to call back.

        Usage:
            ep = ToolBase.endpoint(session)
        """
        return session.get('endpoint_id', '')

    # ── Context injection helpers ─────────────────────────────────────────────

    @staticmethod
    def context_for_modes(core, text: str, modes: tuple = ('plain', 'speaker', 'phone')) -> str:
        """
        Return context text only when the current session interface matches
        one of the specified modes. Pass this from provide_context().

        Modes map to interfaces:
            'plain'   → telegram, web, headless
            'speaker' → voice_remote
            'phone'   → asterisk

        Usage:
            def provide_context(core, tool_config):
                return ToolBase.context_for_modes(
                    core,
                    text  = "[CONTACTS]\\nJesse: jesse@example.com",
                    modes = ('plain', 'speaker', 'phone'),
                )
        """
        # provide_context is called without a session so we return for all
        # modes by default. Tool authors can filter by checking core state
        # if needed — but most context is relevant everywhere.
        return text.strip() if text else ""