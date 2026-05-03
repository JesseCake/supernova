"""
tools/_example_tool.py — Reference template for new Supernova tools.

The underscore prefix means the tool_loader skips this file — it is purely
a guide and copy-paste starting point.

Copy this file, rename it (without the underscore), and fill in your logic.
Delete sections you don't need.

─────────────────────────────────────────────────────────────────────────────
QUICK REFERENCE — what every tool can do:

    from core.tool_base import ToolBase

    log = ToolBase.logger('my_tool')           # logging
    params  = ToolBase.params(tool_args)       # get LLM arguments
    speaker = ToolBase.speaker(session)        # who is speaking ("Jesse" or None)
    iface   = ToolBase.interface(session)      # which interface ("speaker", "general" etc)
    ep      = ToolBase.endpoint(session)       # endpoint id ("supernova-voice", chat_id etc)

    ToolBase.speak(core, session, "...")       # immediate feedback before LLM responds (eg "Checking time. ")
    ToolBase.schedule(...)                     # future callback / reminder / timer
    ToolBase.result(core, 'name', {...})       # return success to LLM
    ToolBase.error(core, 'name', "msg")        # return error to LLM

─────────────────────────────────────────────────────────────────────────────
YAML SIDECAR — automatically loaded from config/{tool_filename}.yaml
No code needed to load it — tool_config is passed into execute() and all
hook functions by the tool loader. All fields are optional.

    enabled:               true   # false to disable without deleting the file
    interfaces:            []     # [] = all, or restrict: [speaker, phone, general]
    agent_modes:           []     # [] = general mode only, [all] = every mode
    blocked_modes:         []     # exclude from specific modes e.g. [relay]
    requires_config:       ""     # skip if AppConfig doesn't have this attribute
    context_priority:      50     # position in system prompt — lower = earlier
    turn_context_priority: 50     # position in per-turn injection order — lower = earlier

    # Any extra fields are yours — passed to execute() and all hook functions
    my_setting: "some value"
    api_key:    ""
    debug:      false

─────────────────────────────────────────────────────────────────────────────
SINGLE TOOL FILE — one schema function + one execute():

    def my_tool(param: str): ...       ← schema (shown to LLM, name must match)
    def execute(...): ...              ← implementation

MULTI-TOOL FILE — multiple tools in one file, exported via TOOLS list:

    def tool_one(param: str): ...
    def tool_two(param: str): ...
    def _exec_one(...): ...
    def _exec_two(...): ...
    TOOLS = [
        {'name': 'tool_one', 'schema': tool_one, 'execute': _exec_one},
        {'name': 'tool_two', 'schema': tool_two, 'execute': _exec_two},
    ]

    Plugin hooks (provide_context etc) work normally in multi-tool files —
    export them at module level alongside the TOOLS list.

─────────────────────────────────────────────────────────────────────────────
PLUGIN HOOKS (all optional — delete any you don't need):

    provide_context(core, tool_config, session) -> str
        Injects static text into the system prompt every turn.
        Cache-safe — does not bust the Ollama KV cache.
        Use for: capability descriptions, persistent rules, known entities.
        Controlled by context_priority in yaml.

    provide_turn_context(core, tool_config, session, user_input) -> str | None
        Injects dynamic text as a system message just before the user message.
        Fires every turn. Return None to inject nothing.
        Use for: per-turn memory retrieval, live state, recent session hints.
        Controlled by turn_context_priority in yaml.

    on_session_end(core, tool_config, session) -> None
        Fires when a session closes cleanly (hangup, timeout, reset).
        Runs in a daemon thread — never blocks the interface.
        Use for: summaries, flushing logs, persisting session state.
─────────────────────────────────────────────────────────────────────────────
"""

from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

# Get a logger for this tool — appears as supernova.tools.my_tool in logs
log = ToolBase.logger('my_tool')


# ── Schema function ───────────────────────────────────────────────────────────
# This is what the LLM sees. The function name, parameter names, type
# annotations, and docstring all become the tool definition sent to Ollama.
# The body should be empty (just ...) — the actual logic is in execute().
#
# IMPORTANT: the function name must match the tool name exactly.
# The loader uses __name__ to register it with Ollama.
#
# Use Annotated + Field for rich parameter descriptions:
#   param: Annotated[str, Field(description="What this param is for")]
#
# Use plain type hints for simple parameters:
#   count: int = 3

def my_tool(
    required_param: Annotated[str, Field(
        description="What this parameter is for. Required.",
    )],
    optional_param: Annotated[str, Field(
        default="",
        description="Optional detail — leave empty to use the default.",
    )] = "",
    count: int = 1,
) -> str:
    """
    One-line description of what this tool does.

    The LLM reads this docstring to decide when to use the tool.
    Be specific — include the kinds of user requests that should trigger it.
    Example: "Use when the user asks about X, Y, or Z."
    Include example phrases if helpful:
    'what is X', 'show me X', 'how do I X'.
    """
    ...   # body intentionally empty — logic is in execute()


# ── Static system prompt injection (optional) ─────────────────────────────────
# Called every turn to inject text into the static system prompt.
# Text here is cache-safe and does not change turn-to-turn.
# Use for things the LLM should always know (contacts, capability descriptions).
# Return "" to inject nothing. Remove this function if not needed.

def provide_context(core, tool_config: dict, session: dict) -> str:
    """Inject relevant context into the system prompt on every turn."""
    if not tool_config.get('enabled', True):
        return ""
    some_setting = tool_config.get('my_setting', '')
    if not some_setting:
        return ""
    return f"[MY TOOL]\nSome context the LLM should always know: {some_setting}"


# ── Per-turn injection (optional) ─────────────────────────────────────────────
# Called every turn, injected as a system message just before the user message.
# Unlike provide_context, this can vary based on user_input.
# Does not bust the Ollama KV cache — safe to use every turn.
# Return None to inject nothing this turn. Remove this function if not needed.

def provide_turn_context(core, tool_config: dict, session: dict, user_input: str) -> str | None:
    """Inject dynamic context just before the user message each turn."""
    if not tool_config.get('enabled', True):
        return None
    # ... query something based on user_input ...
    # return "[CONTEXT]\nSomething relevant to this specific turn."
    return None


# ── Session end hook (optional) ───────────────────────────────────────────────
# Called when a session closes cleanly (hangup, timeout, /reset).
# Runs in a daemon thread — never blocks the interface.
# Use for: summaries, flushing logs, persisting end-of-session state.
# Remove this function if not needed.

def on_session_end(core, tool_config: dict, session: dict):
    """Clean up or persist state when a session ends."""
    if not tool_config.get('enabled', True):
        return
    # ... generate a summary, flush logs, etc. ...
    pass


# ── Executor ──────────────────────────────────────────────────────────────────
# Called when the LLM invokes this tool. Receives:
#   tool_args   — {'name': 'my_tool', 'parameters': {'required_param': '...', ...}}
#   session     — the current session dict
#   core        — the CoreProcessor instance
#   tool_config — the parsed yaml sidecar dict
#
# Must return ToolBase.result(...) or ToolBase.error(...).
# Returning None is also valid — the LLM receives "ok" as the result.

def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:

    # ── Extract parameters ────────────────────────────────────────────────────
    params         = ToolBase.params(tool_args)
    required_param = params.get('required_param', '').strip()
    optional_param = params.get('optional_param', '').strip()
    count          = int(params.get('count', 1))

    log.info("Executing my_tool", extra={'data': f"param={required_param!r}"})

    # ── Validate ──────────────────────────────────────────────────────────────
    if not required_param:
        return ToolBase.error(core, 'my_tool', "required_param was not provided.")

    # ── Validate required config (if your tool needs it) ──────────────────────
    # err = ToolBase.require_config(tool_config, 'api_key')
    # if err:
    #     return ToolBase.error(core, 'my_tool', err)

    # ── Session context ───────────────────────────────────────────────────────
    speaker   = ToolBase.speaker(session)    # e.g. "Jesse" or None
    interface = ToolBase.interface(session)  # e.g. "speaker", "general"
    endpoint  = ToolBase.endpoint(session)   # e.g. "supernova-voice", "286047661"

    log.debug("Session context",
              extra={'data': f"speaker={speaker} interface={interface}"})

    # ── Adapt to interface ────────────────────────────────────────────────────
    # Voice responses should be short and spoken-word friendly.
    # Text interfaces (Telegram, web) can use more detail and formatting.
    # if interface == 'speaker':
    #     # keep it brief
    # else:
    #     # can be more detailed

    # ── Immediate feedback (optional) ─────────────────────────────────────────
    # Heard/seen by the user immediately, before the LLM generates its response.
    # Use for slow operations so the user knows something is happening.
    ToolBase.speak(core, session, "Looking that up now.")

    # ── Schedule a future callback (optional) ─────────────────────────────────
    # Use for timers, reminders, or any future event that should reach the user.
    # event_id = ToolBase.schedule(
    #     core, session, tool_config,
    #     label         = "my reminder",
    #     delay_seconds = 300,
    #     announcement  = "Your reminder is ready. Announce this naturally.",
    #     event_type    = 'reminder',
    # )

    # ── Do the actual work ────────────────────────────────────────────────────
    try:
        result_value = f"processed {required_param}"   # replace with real logic

        return ToolBase.result(core, 'my_tool', {
            "status":       "success",
            "result":       result_value,
            # 'instructions' guides the LLM's response wording — be explicit
            # to prevent the LLM from reading out IDs, reformatting data etc.
            "instructions": f"Tell the user the result was: {result_value}.",
        })

    except Exception as e:
        log.error("my_tool failed", exc_info=True)
        return ToolBase.error(core, 'my_tool', f"Something went wrong: {e}")