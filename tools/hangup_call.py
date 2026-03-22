"""
hangup_call tool — signals the voice channel to close.
Voice-only (set in hangup_call.yaml).
"""
from core.tool_base import ToolBase

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
    session['close_voice_channel'].set()
    # Returning None tells core the tool ran successfully with no result text
    return None