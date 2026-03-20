"""
hangup_call tool — signals the voice channel to close.
Voice-only (set in hangup_call.yaml).
"""


# ── Schema function ───────────────────────────────────────────────────────────

def hangup_call() -> str:
    """
    End and hang up the current voice call.
    Use this when the user says goodbye, asks to end the call, or the conversation is clearly finished.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    # Signal the voice interface to close the channel
    session['close_voice_channel'].set()
    # No return value needed — hangup is handled by the caller checking tool_name
    return None