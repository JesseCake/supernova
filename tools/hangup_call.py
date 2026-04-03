"""
hangup_call tool — signals the voice channel to close.
Restricted to voice interfaces only (set in hangup_call.yaml).
"""
from core.tool_base import ToolBase
from core.session_state import request_hangup

log = ToolBase.logger('hangup_call')


# ── Schema function ───────────────────────────────────────────────────────────

def hangup_call() -> str:
    """
    End and hang up the current voice call.
    Use this when the user says goodbye, asks to end the call, or the conversation is clearly finished.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("Hanging up", extra={'data': f"endpoint={ToolBase.endpoint(session)!r}"})
    # Signal the voice interface to close the channel after this response
    request_hangup(session)
    return None