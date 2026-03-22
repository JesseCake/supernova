"""
tools/_example_tool.py — Reference template for new Supernova tools.

The underscore prefix means the tool_loader skips this file — it is purely
a guide and copy-paste starting point.

Copy this file, rename it (without the underscore), and fill in your logic.
Delete sections you don't need.

─────────────────────────────────────────────────────────────────────────────
QUICK REFERENCE — what every tool can do:

    from core.tool_base import ToolBase

    log = ToolBase.logger('my_tool')         # logging
    params  = ToolBase.params(tool_args)     # get LLM arguments
    speaker = ToolBase.speaker(session)      # who is speaking
    iface   = ToolBase.interface(session)    # which interface (telegram etc)

    ToolBase.speak(core, session, "...")     # immediate feedback
    ToolBase.schedule(...)                   # future callback
    ToolBase.result(core, 'name', {...})     # return success to LLM
    ToolBase.error(core, 'name', "msg")      # return error to LLM

─────────────────────────────────────────────────────────────────────────────
# YAML SIDECAR — automatically loaded from config/{tool_filename}.yaml
# No code needed to load it — tool_config is passed directly into execute()
# and provide_context() by the tool loader. All fields are optional.

    enabled:          true    # false to disable without deleting the file
    voice_only:       false   # true = only on satellite voice (VoiceMode.SPEAKER)
    phone_only:       false   # true = only on phone calls (VoiceMode.PHONE)
    requires_config:  ""      # skip if AppConfig doesn't have this attribute set
    context_priority: 50      # lower = earlier in system prompt (default 50)

    # Any extra fields are passed to execute() and provide_context() as tool_config
    my_setting: "some value"
    api_key:    ""

─────────────────────────────────────────────────────────────────────────────
SINGLE TOOL FILE — one schema function + one execute():

    def my_tool(param: str): ...       ← schema (shown to LLM)
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
# Use Annotated + Field for rich parameter descriptions:
#   param: Annotated[str, Field(description="What this param is for")]
#
# Or plain type hints for simple cases:
#   param: str = "default"

def my_tool(
    required_param: Annotated[str, Field(description="What this parameter is for. Required.")],
    optional_param: Annotated[str, Field(default="", description="Optional detail.")] = "",
    count: int = 1,
) -> str:
    """
    One-line description of what this tool does.

    The LLM reads this docstring to decide when to use the tool.
    Be specific — include the kinds of user requests that should trigger it,
    e.g. "Use when the user asks about X, Y, or Z."

    Args:
        required_param: Description of required_param.
        optional_param: Description of optional_param.
        count:          How many results to return.
    """
    ...   # body intentionally empty — logic is in execute()


# ── Context provider (optional) ───────────────────────────────────────────────
# If present, called on every LLM request to inject text into the system prompt.
# Use for things the LLM should always know about (contacts list, current state).
# Return an empty string to inject nothing (e.g. if config is missing).
# Remove this function entirely if your tool has nothing to inject.

def provide_context(core, tool_config: dict) -> str:
    """Inject relevant context into the system prompt on every turn."""
    some_setting = tool_config.get('my_setting', '')
    if not some_setting:
        return ""
    return f"[MY TOOL]\nSome context the LLM should know: {some_setting}"


# ── Executor ──────────────────────────────────────────────────────────────────
# Called when the LLM invokes this tool. Receives:
#   tool_args   — {'name': 'my_tool', 'parameters': {'required_param': '...', ...}}
#   session     — the current session dict (endpoint_id, interface, speaker, etc.)
#   core        — the CoreProcessor instance (schedule_event, send_whole_response, etc.)
#   tool_config — the parsed yaml sidecar dict (your custom config fields)
#
# Must return either ToolBase.result(...) or ToolBase.error(...).
# Returning None is also valid — the LLM gets "ok" as the result.

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

    # ── Session context ───────────────────────────────────────────────────────
    speaker   = ToolBase.speaker(session)    # e.g. "Jesse" or None
    interface = ToolBase.interface(session)  # e.g. "telegram", "voice_remote"
    endpoint  = ToolBase.endpoint(session)   # e.g. "286047661", "supernova-voice"

    log.debug("Session context", extra={'data': f"speaker={speaker} interface={interface}"})

    # ── Immediate feedback (optional) ─────────────────────────────────────────
    # Heard/seen immediately, before the LLM generates its response.
    # Use for slow operations so the user knows something is happening.
    ToolBase.speak(core, session, "Looking that up now.")

    # ── Schedule a future callback (optional) ─────────────────────────────────
    # Use for timers, reminders, or any future event that should reach the user.
    # event_id = ToolBase.schedule(
    #     core, session, tool_config,
    #     label         = "my reminder",
    #     delay_seconds = 300,
    #     announcement  = "Your reminder is ready.",
    #     event_type    = 'reminder',
    # )

    # ── Do the actual work ────────────────────────────────────────────────────
    try:
        result_value = f"processed {required_param}"   # replace with real logic

        return ToolBase.result(core, 'my_tool', {
            "status":       "success",
            "result":       result_value,
            # 'instructions' guides the LLM's response wording — optional but useful
            "instructions": f"Tell the user the result was: {result_value}.",
        })

    except Exception as e:
        log.error("my_tool failed", exc_info=True)
        return ToolBase.error(core, 'my_tool', f"Something went wrong: {e}")