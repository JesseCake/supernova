"""
tools/send_email.py — Send an email via SMTP.
Config: config/send_email.yaml

Recipient resolution priority:
  1. Friendly name matched against contacts dict in yaml (e.g. "Jesse" -> "jesse@example.com")
  2. Raw email address passed directly (if it contains @)
  3. Identified speaker's enrolled email (from speaker_profiles.json)
  4. default_to in yaml
  5. Error if none found
"""
from typing import Annotated
from pydantic import Field
from core.tool_base import ToolBase

log = ToolBase.logger('send_email')


# ── Schema ────────────────────────────────────────────────────────────────────

def send_email(
    subject: Annotated[str, Field(description="The email subject line. Required.")],
    body:    Annotated[str, Field(description="The email body content. Required.")],
    to_address: Annotated[str, Field(
        default="",
        description=(
            "Recipient name (friendly name from known contacts if the user specifies one) or email address. "
            "Leave empty to send to the identified speaker (if it seems they're asking you to send something to them for later etc)."
        ),
    )] = "",
) -> str:
    """
    Send an email via SMTP.
    Use when the user asks to send email to someone.
    If no recipient is specified and a speaker is identified, this tool will send to them.
    """
    ...


# ── Context provider ──────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict, session: dict) -> str:
    """Inject known contact names into the system prompt so the LLM can use them."""
    contacts = tool_config.get('contacts', {})
    if not contacts:
        return ""
    names = ", ".join(contacts.keys())
    return f"[EMAIL CONTACTS]\nKnown email contacts (use these names as recipients): {names}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_address(name_or_email: str, tool_config: dict) -> str:
    """
    Resolve a friendly name or raw email to a real email address.
    Checks the contacts dict in tool_config first (case-insensitive),
    then returns as-is if it looks like a real email address.
    """
    if not name_or_email:
        return ""
    contacts = tool_config.get('contacts', {})
    for contact_name, email in contacts.items():
        if contact_name.lower() == name_or_email.lower():
            return email
    if '@' in name_or_email:
        return name_or_email
    return ""


# ── Executor ──────────────────────────────────────────────────────────────────

def execute(tool_args: dict, session: dict, core, tool_config: dict) -> str:
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    # Validate required config up front
    err = ToolBase.require_config(tool_config, 'smtp_host', 'username', 'password')
    if err:
        return ToolBase.error(core, 'send_email', err)

    params  = ToolBase.params(tool_args)
    subject = params.get('subject', '').strip()
    body    = params.get('body', '').strip()

    if not subject:
        return ToolBase.error(core, 'send_email', "No subject provided.")
    if not body:
        return ToolBase.error(core, 'send_email', "No body provided.")

    # ── Recipient resolution ──────────────────────────────────────────────────

    # 1. Friendly name or raw address from tool call
    to_address = _resolve_address(params.get('to_address', '').strip(), tool_config)

    # 2. Fall back to identified speaker's enrolled email
    if not to_address:
        speaker = ToolBase.speaker(session)
        if speaker:
            profiles  = ToolBase.read_json('speaker_id', 'speaker_profiles.json', default={})
            to_address = profiles.get(speaker, {}).get('email', '')

    # 3. Fall back to default_to in config
    if not to_address:
        to_address = _resolve_address(tool_config.get('default_to', ''), tool_config)

    if not to_address:
        return ToolBase.error(core, 'send_email',
            "No recipient found. Specify a contact name or email address, "
            "or enroll the speaker with an email address."
        )

    # ── Send ──────────────────────────────────────────────────────────────────

    ToolBase.speak(core, session, f"Sending email to {to_address}. ")
    log.info("Sending email", extra={'data': f"to={to_address} subject={subject!r}"})

    smtp_host = tool_config.get('smtp_host', 'smtp.gmail.com')
    smtp_port = int(tool_config.get('smtp_port', 587))
    username  = tool_config.get('username', '')
    password  = tool_config.get('password', '')
    from_addr = tool_config.get('from_address', username)

    msg            = MIMEMultipart()
    msg['From']    = from_addr
    msg['To']      = to_address
    msg['Subject'] = subject
    body += "\n\nSupernova\n(I am machine intelligence, I try to do good, but sometimes make mistakes)"
    msg.attach(MIMEText(body, 'plain'))

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                if username and password:
                    server.login(username, password)
                server.sendmail(from_addr, to_address, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if username and password:
                    server.starttls()
                    server.login(username, password)
                server.sendmail(from_addr, to_address, msg.as_string())

        log.info("Email sent", extra={'data': f"to={to_address}"})
        return ToolBase.result(core, 'send_email', {
            "status":       "sent",
            "to":           to_address,
            "subject":      subject,
            "instructions": (
                f"Tell the user the email was sent to {to_address} "
                f"with subject '{subject}'."
            ),
        })

    except smtplib.SMTPAuthenticationError:
        log.error("SMTP authentication failed")
        return ToolBase.error(core, 'send_email',
            "Authentication failed — check the SMTP username and password in config."
        )
    except smtplib.SMTPException as e:
        log.error(f"SMTP error: {e}", exc_info=True)
        return ToolBase.error(core, 'send_email', f"SMTP error: {e}")
    except Exception as e:
        log.error("Unexpected error sending email", exc_info=True)
        return ToolBase.error(core, 'send_email', f"Failed to send email: {e}")