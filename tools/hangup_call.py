"""
hangup_call tool — signals the voice channel to close.
Restricted to voice interfaces only (set in hangup_call.yaml).
"""
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase
from core.session_state import request_hangup

log = ToolBase.logger('hangup_call')


# ── Schema function ───────────────────────────────────────────────────────────

def hangup_call(
    farewell: Annotated[str, Field(
        default="",
        description=(
            "Optional short goodbye to speak as the call ends, e.g. "
            "'No worries, bye!'. Put your goodbye HERE, not in response text."
        )
    )] = "",
) -> str:
    """
    End the current voice call, optionally speaking a short farewell.
    Use when the request is fully resolved, or the user says goodbye,
    'that's all', or 'thanks'. Call this INSTEAD of writing a goodbye —
    saying 'I'll hang up now' in text does not end the call; only this
    tool does.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    farewell = (ToolBase.params(tool_args).get('farewell') or '').strip()
    log.info("Hanging up", extra={'data': f"endpoint={ToolBase.endpoint(session)!r} farewell={farewell!r}"})
    if farewell:
        ToolBase.speak(core, session, farewell)
    request_hangup(session)
    return None