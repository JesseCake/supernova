"""
contact_user tool — proactively contact a known user via their best available
interface and collect a response on behalf of the current caller.

Creates a relay session on the target user's interface with a restricted
toolset (reply_to_caller only). If the user has an active session, it is
suspended and restored after the relay completes.

Interfaces are reached generically via core.get_interface(name) —
adding a new interface to the system requires no changes here.

Config (config/contact_user.yaml):
    enabled: true
    agent_modes: [all]
"""

import uuid
import asyncio
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase
from core.interface_mode import InterfaceMode
from core.session_state import KEY_INTERFACE_MODE, KEY_AGENT_MODE

log = ToolBase.logger('contact_user')


# ── Schema ────────────────────────────────────────────────────────────────────

def contact_user(
    user: Annotated[str, Field(
        description=(
            "The user to contact. Use their name as defined in user_profiles "
            "e.g. 'jesse' or 'dean'."
        )
    )],
    message: Annotated[str, Field(
        description=(
            "The question or message to relay to the user. "
            "Write it as you would say it to them directly, "
            "e.g. 'Jesse wants to know what you would like for dinner tonight.'"
        )
    )],
    preferred_interface: Annotated[str, Field(
        default="",
        description=(
            "Optional. The interface to use: 'telegram', 'speaker', 'email'. "
            "Also accepts natural language: 'IM', 'message', 'voice', 'mail'. "
            "IMPORTANT: Leave empty to use the user's preferred contact method automatically unless asked by user."
        )
    )] = "",
) -> str:
    """
    Contact another user on their preferred interface and collect a response.
    Use when asked to check with someone, ask someone something, or relay a
    question to another person.
    Examples: 'ask Dean what he wants for dinner',
              'check with Jesse if he's coming home',
              'message Dean on Telegram and ask him about the shopping'.
    """
    ...


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    params    = ToolBase.params(tool_args)
    user_id   = (params.get('user') or '').strip().lower()
    message   = (params.get('message') or '').strip()
    preferred = (params.get('preferred_interface') or '').strip() or None

    if not user_id:
        return ToolBase.error(core, 'contact_user', "No user specified.")
    if not message:
        return ToolBase.error(core, 'contact_user', "No message specified.")

    if not hasattr(core, 'presence_registry'):
        return ToolBase.error(core, 'contact_user',
            "Presence registry not available. Do not retry.")

    registry = core.presence_registry

    if preferred:
        preferred = registry.normalise_interface(preferred)

    # Resolve best contact method
    result = registry.get_best_contact(user_id, preferred=preferred)
    if result is None:
        friendly  = registry.get_friendly_name(user_id)
        all_users = registry.all_users()
        if user_id not in all_users:
            return ToolBase.error(core, 'contact_user',
                f"Unknown user '{user_id}'. "
                f"Known users: {', '.join(all_users)}. Do not retry.")
        if preferred:
            return ToolBase.error(core, 'contact_user',
                f"{friendly} is not configured for '{preferred}'. "
                f"Try without specifying preferred_interface instead.")
        return ToolBase.error(core, 'contact_user',
            f"No contact method available for {friendly}. "
            f"Tell the user you were unable to reach {friendly} and do not retry.")

    interface, details  = result
    friendly            = registry.get_friendly_name(user_id)
    caller_name         = ToolBase.speaker(session) or "Someone"
    caller_session_id   = _get_session_id(core, session)
    caller_endpoint     = ToolBase.endpoint(session)
    caller_interface    = ToolBase.interface(session)

    log.info("Contacting user",
             extra={'data': f"{user_id} via {interface} details={details}"})

    # Email is fire-and-forget — no relay session needed
    if interface == 'email':
        return _route_email(core, session, details, message, friendly, caller_name)

    # Get the registered interface object
    iface_obj = core.get_interface(interface)
    if iface_obj is None:
        return ToolBase.error(core, 'contact_user',
            f"Interface '{interface}' is not registered. "
            f"Is it enabled in config?")

    # Resolve endpoint identifier for this interface
    endpoint_id = details.get('endpoint_id') or details.get('chat_id', '')
    if not endpoint_id:
        return ToolBase.error(core, 'contact_user',
            f"No endpoint identifier found for {friendly} on {interface}.")

    # Create relay session
    relay_session_id = str(uuid.uuid4())
    relay_session    = core.create_session(relay_session_id)

    relay_session[KEY_INTERFACE_MODE]         = InterfaceMode.GENERAL
    relay_session['interface']                = interface
    relay_session['endpoint_id']              = endpoint_id
    relay_session['relay_question']           = message
    relay_session['relay_caller']             = caller_name
    relay_session['relay_caller_session_id']  = caller_session_id
    relay_session['relay_caller_endpoint']    = caller_endpoint
    relay_session['relay_caller_interface']   = caller_interface
    relay_session['relay_target_user']        = user_id
    relay_session['relay_target_interface']   = interface
    relay_session['relay_target_details']     = details

    # Set relay agent mode
    relay_mode = core.mode_registry.get('relay')
    if relay_mode:
        relay_session[KEY_AGENT_MODE] = relay_mode

    # Pre-populate history so Supernova has context when Dean's reply arrives.
    # The opening message is already delivered by send_relay_message below —
    # we just record it in history so the LLM sees the full exchange.
    from core.session_state import get_history
    opening = f"{caller_name} wants to ask you: {message}"
    get_history(relay_session).append({
        'role':    'system',
        'content': (
            f"[RELAY IDENTITY]\n"
            f"You are speaking with {friendly}.\n"
            f"{caller_name} has asked you to relay a question to {friendly}.\n"
            f"Do not call {friendly} by any other name. "
            f"Do not confuse {friendly} with {caller_name}."
        ),
    })
    get_history(relay_session).append({
        'role':    'assistant',
        'content': opening,
    })

    # Set up immediate_send for relay session responses
    _setup_immediate_send(relay_session, iface_obj, interface, endpoint_id, core)

    # Push any existing session to the stack
    log.info("Pushing existing session to stack",
             extra={'data': f"{friendly} {interface} endpoint={endpoint_id}"})
    iface_obj.push_session(endpoint_id)

    # Activate relay session on this endpoint
    log.info("Activating relay session",
             extra={'data': f"relay_session={relay_session_id} endpoint={endpoint_id}"})
    iface_obj.set_relay_session(endpoint_id, relay_session_id)

    # Deliver the opening message
    log.info("Sending relay opening message",
             extra={'data': f"to={friendly} endpoint={endpoint_id}"})
    iface_obj.send_relay_message(endpoint_id, opening)

    log.info("Relay initiated",
             extra={'data': f"{friendly} via {interface} endpoint={endpoint_id}"})

    return ToolBase.result(core, 'contact_user', {
        "status":       "sent",
        "user":         friendly,
        "interface":    interface,
        "instructions": (
            f"Tell the user you've reached out to {friendly} via "
            f"{interface} and will let them know when you get a reply."
        ),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup_immediate_send(relay_session: dict, iface_obj, interface: str,
                          endpoint_id: str, core):
    """
    Wire up immediate_send on the relay session so the LLM's responses
    stream back to the relay user immediately.
    """
    loop = getattr(core, '_loop', None)
    if loop is None:
        return

    if interface == 'telegram' and hasattr(iface_obj, 'send_message'):
        relay_session['immediate_send'] = lambda text, _l=loop, _e=endpoint_id: \
            asyncio.run_coroutine_threadsafe(
                iface_obj.send_message(_e, text), _l
            )
        relay_session['immediate_send_only'] = True


def _route_email(core, session, details: dict, message: str,
                 friendly: str, caller: str) -> str:
    """Send relay via email — fire and forget, no relay session needed."""
    address = details.get('address', '')
    if not address:
        return ToolBase.error(core, 'contact_user',
            f"No email address for {friendly}.")

    fn = core.tool_loader.get_executor('send_email')
    if fn is None:
        return ToolBase.error(core, 'contact_user',
            "send_email tool not available.")
    try:
        fn(
            tool_args = {
                'name':       'send_email',
                'parameters': {
                    'to_address': address,
                    'subject':    f"Message from {caller} via Supernova",
                    'body':       message,
                }
            },
            session = session,
            core    = core,
        )
        return ToolBase.result(core, 'contact_user', {
            "status":       "sent",
            "user":         friendly,
            "interface":    "email",
            "instructions": (
                f"Tell the user you've emailed {friendly} at {address}. "
                f"Note that email doesn't support a live reply."
            ),
        })
    except Exception as e:
        log.error("Email relay failed", exc_info=True)
        return ToolBase.error(core, 'contact_user',
            f"Failed to email {friendly}: {e}")


def _get_session_id(core, session: dict) -> str | None:
    """Reverse-lookup the session_id from a session dict."""
    for sid, s in core.sessions.items():
        if s is session:
            return sid
    return None