"""
end_call tool — signals the voice channel to close.
Restricted to voice interfaces only (set in end_call.yaml).
"""
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase
from core.session_state import request_hangup

log = ToolBase.logger('end_call')


# ── Schema function ───────────────────────────────────────────────────────────

def end_call() -> str:
    """
    End the voice call. Use when the request is resolved or the user says goodbye, 'that's all', 'thanks' etc. Say a brief goodbye in your reply text and call this tool in the same turn; saying 'I'll hang up' without calling it leaves the call open.
    """

# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    log.info("Hanging up", extra={'data': f"endpoint={ToolBase.endpoint(session)!r}"})
    ToolBase.request_hangup(session)
    return None