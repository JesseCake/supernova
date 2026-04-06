"""
reply_to_caller tool — close a relay session and return the answer to the caller.

Only available in relay agent mode. Called by the relayed user when they
have answered the question. Injects the reply into the caller's session,
restores any suspended session on the relay user's interface, and injects
a context note so both sides know what happened.

Config (config/reply_to_caller.yaml):
    enabled: true
    agent_modes: [relay]
"""

import threading
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase
from core.session_state import get_history

log = ToolBase.logger('reply_to_caller')

# Sentinel value meaning the wrong person answered
WRONG_PERSON = "WRONG_PERSON"


# ── Schema ────────────────────────────────────────────────────────────────────

def reply_to_caller(
    message: Annotated[str, Field(
        description=(
            "The reply to the question. Send exactly the respondant's answer as you would say it naturally, without mentioning the relay or instructions. "
            "If they not the intended recipient, send 'WRONG_PERSON'."
        )
    )],
) -> str:
    """
    Send your reply back to the person who asked the question.
    Use this as soon as you have an answer — do not delay.
    If you find you have reached the wrong person, send message='WRONG_PERSON'.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    message = (ToolBase.params(tool_args).get('message') or '').strip()

    if not message:
        return ToolBase.error(core, 'reply_to_caller', "No message provided.")

    # Read relay metadata from session
    caller_session_id   = session.get('relay_caller_session_id')
    caller_name         = session.get('relay_caller', 'the caller')
    caller_interface    = session.get('relay_caller_interface', '')
    caller_endpoint     = session.get('relay_caller_endpoint', '')
    target_user         = session.get('relay_target_user', 'unknown')
    target_interface    = session.get('relay_target_interface', '')
    target_details      = session.get('relay_target_details', {})
    question            = session.get('relay_question', '')

    target_friendly = core.presence_registry.get_friendly_name(target_user) \
        if hasattr(core, 'presence_registry') else target_user

    # ── Handle wrong person ───────────────────────────────────────────────────
    if message == WRONG_PERSON:
        log.info("Wrong person responded",
                 extra={'data': f"endpoint={ToolBase.endpoint(session)}"})

        # Mark this contact method unavailable temporarily
        if hasattr(core, 'presence_registry'):
            core.presence_registry.mark_unavailable(target_user, target_interface, ttl=600)

        _restore_target_session(core, session, context_note=(
            f"[RELAY INTERRUPTED]\n"
            f"A relay message was sent to this endpoint but the wrong person responded. "
            f"The relay has been cancelled. Resume your conversation naturally."
        ))

        _inject_into_caller(core, caller_session_id,
            f"{target_friendly} could not be reached — the wrong person answered. "
            f"The relay has been cancelled."
        )

        return ToolBase.result(core, 'reply_to_caller', {
            "status": "wrong_person",
            "instructions": "Tell the user they are not the intended recipient and end the conversation.",
        })

    # ── Inject reply into caller's session ────────────────────────────────────
    log.info("Relay reply received",
             extra={'data': f"from={target_friendly} message={message!r}"})

    relay_message = (
        f"[RELAY REPLY]\n"
        f"{target_friendly} replied to the question '{question}':\n"
        f"\"{message}\"\n"
        f"Relay the answer to {caller_name} naturally."
    )
    _inject_into_caller(
        core, 
        caller_session_id, 
        relay_message,
        caller_interface = caller_interface,
        caller_endpoint  = caller_endpoint,
        )

    # ── Restore target's suspended session ────────────────────────────────────
    context_note = (
        f"[RELAY COMPLETED]\n"
        f"While this conversation was paused, {caller_name} asked: '{question}'. "
        f"They replied: '{message}'. This has been passed back to {caller_name}. "
        f"Resume your conversation naturally — do not mention this unless brought up."
    )
    _restore_target_session(core, session, context_note=context_note)

    return ToolBase.result(core, 'reply_to_caller', {
        "status":       "sent",
        "instructions": (
            f"Thank {target_friendly} for their reply and end the conversation naturally."
        ),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_into_caller(core, caller_session_id: str, message: str,
                        caller_interface: str = '', caller_endpoint: str = ''):
    if not caller_session_id:
        log.warning("No caller session id — cannot inject reply")
        return

    caller_session = core.get_session(caller_session_id)

    if caller_session is None:
        # Session is gone (caller hung up). If we have routing info,
        # fire the event handler directly — same path as the timer callback.
        log.warning("Caller session gone — attempting callback via event handler",
                    extra={'data': f"interface={caller_interface!r} endpoint={caller_endpoint!r}"})
        if caller_interface and caller_endpoint:
            handler = core._event_handlers.get(caller_interface)
            if handler:
                handler({
                    'endpoint_id':  caller_endpoint,
                    'announcement': message,
                    'missed':       False,
                })
            else:
                log.warning("No event handler for caller interface",
                            extra={'data': caller_interface})
        return

    # Session still alive — inject normally
    log.info("Injecting reply into caller session",
             extra={'data': caller_session_id})

    thread = threading.Thread(
        target  = core.process_input,
        kwargs  = {
            'input_text': message,
            'session_id': caller_session_id,
        },
        daemon  = True,
    )
    thread.start()


def _restore_target_session(core, relay_session: dict, context_note: str = None):
    """
    Close the relay session and restore the target user's previous session
    on their interface.
    """
    target_interface = relay_session.get('relay_target_interface', '')
    target_details   = relay_session.get('relay_target_details', {})

    endpoint_id = target_details.get('endpoint_id') or target_details.get('chat_id', '')
    if not endpoint_id:
        return

    iface_obj = core.get_interface(target_interface)
    if iface_obj and hasattr(iface_obj, 'pop_session'):
        iface_obj.pop_session(endpoint_id, context_note=context_note)
        log.info("Session restored",
                 extra={'data': f"interface={target_interface} endpoint={endpoint_id}"})