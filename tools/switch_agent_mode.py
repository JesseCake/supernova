"""
switch_agent_mode tool — switches Supernova's active agent mode mid-session.

Agent modes define personality and focus (e.g. general, deep_research,
document_analysis, transcription). Switching takes effect on the next
turn — the new personality file is loaded and max_tool_loops changes.

The available mode names are injected dynamically into the schema description
via the get_schema hook so the LLM always knows what modes exist.

Config (config/switch_agent_mode.yaml):
    enabled: true
"""

from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('switch_agent_mode')


# ── Dynamic schema hook ───────────────────────────────────────────────────────

def get_schema(tool_config: dict, core) -> callable:
    """
    Called by tool_loader to get the schema function.
    Builds the schema with current mode names injected into the description
    so the LLM always sees up-to-date options from modes.yaml.
    """
    if core is not None and hasattr(core, 'mode_registry'):
        names       = core.mode_registry.names()
        default     = core.mode_registry.default().name
        modes_list  = ", ".join(f"'{n}'" for n in names)
        description = (
            f"The agent mode to switch to. Available modes: {modes_list}. "
            f"Default is '{default}'."
        )
    else:
        description = "The agent mode to switch to (you should know the names available from the system prompt)."

    def switch_agent_mode(
        mode: Annotated[str, Field(description=description)],
    ) -> str:
        """
        Switch to a different agent mode.
        Use this when the user asks to change mode —
        for example switching to research mode for a deep investigation,
        or transcription mode to write something up.
        Switch back to general mode when the focused task is complete or requested by the user.
        """
        ...

    return switch_agent_mode


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params    = ToolBase.params(tool_args)
    mode_name = (params.get('mode') or '').strip().lower()

    if not mode_name:
        return ToolBase.error(core, 'switch_agent_mode', "No mode specified.")

    # Look up the mode in the registry
    mode = core.mode_registry.get(mode_name)
    if mode is None:
        available = ", ".join(core.mode_registry.names())
        return ToolBase.error(
            core, 'switch_agent_mode',
            f"Unknown mode '{mode_name}'. Available modes: {available}."
        )

    current = ToolBase.get_agent_mode(session)
    if current == mode_name:
        return ToolBase.result(core, 'switch_agent_mode', {
            "text":        f"Already in {mode_name} mode.",
            "mode":        mode_name,
            "description": mode.description,
        })

    ToolBase.set_agent_mode(session, mode)
    log.info("Agent mode switched", extra={'data': f"{current} → {mode_name}"})

    return ToolBase.result(core, 'switch_agent_mode', {
        "text":         f"Switched to {mode_name} mode.",
        "mode":         mode_name,
        "description":  mode.description,
        "instructions": f"You have switched to {mode_name} mode. {mode.description} Acknowledge the switch briefly and continue.",
    })