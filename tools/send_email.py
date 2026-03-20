"""
send_email tool — sends an email via SMTP.
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


# ── Schema ────────────────────────────────────────────────────────────────────

def send_email(
    subject: Annotated[str, Field(description="The email subject line. Required.")],
    body: Annotated[str, Field(description="The email body content. Required.")],
    to_address: Annotated[str, Field(
        default="",
        description=(
            "Recipient name or email address. Use a friendly name from the known contacts "
            "if the user specifies one (e.g. 'Jesse', 'Dean', 'Mum'). "
            "Leave empty to send to the identified speaker."
        )
    )] = "",
) -> str:
    """
    Send an email via SMTP.
    Use when the user asks to send, email, or message someone.
    If no recipient is specified and a speaker is identified, send to them.
    """
    ...


# ── Context provider ──────────────────────────────────────────────────────────

def provide_context(core, tool_config: dict) -> str:
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

def execute(tool_args: dict, session, core, tool_config: dict) -> str:
    import smtplib
    import os
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from core.speaker_id import load_profiles

    params  = tool_args.get('parameters', {})
    subject = params.get('subject', '').strip()
    body    = params.get('body', '').strip()

    # 1. Resolve friendly name or raw email from tool call
    to_address = _resolve_address(params.get('to_address', '').strip(), tool_config)

    # 2. Fall back to identified speaker's enrolled email
    if not to_address:
        speaker = session.get('speaker')
        if speaker:
            config_dir = os.path.join(os.path.dirname(__file__), '../config')
            profiles   = load_profiles(config_dir)
            profile    = profiles.get(speaker, {})
            to_address = profile.get('email', '')

    # 3. Fall back to default_to in config
    if not to_address:
        to_address = _resolve_address(tool_config.get('default_to', ''), tool_config)

    if not to_address:
        return core._wrap_tool_result("send_email", {
            "text": (
                "No recipient address found. "
                "Please specify a contact name or email address, "
                "or enroll the speaker with an email address."
            )
        })

    if not subject:
        return core._wrap_tool_result("send_email", {"text": "No subject provided."})

    if not body:
        return core._wrap_tool_result("send_email", {"text": "No body provided."})

    core.send_whole_response(f"Sending email to {to_address}...", session)
    core._log("send_email", session=session, extra=f"to={to_address} subject={subject}")

    try:
        smtp_host = tool_config.get('smtp_host', 'smtp.gmail.com')
        smtp_port = tool_config.get('smtp_port', 587)
        username  = tool_config.get('username', '')
        password  = tool_config.get('password', '')
        from_addr = tool_config.get('from_address', username)

        msg = MIMEMultipart()
        msg['From']    = from_addr
        msg['To']      = to_address
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

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

        return core._wrap_tool_result("send_email", {
            "text": f"Email sent to {to_address}.",
            "instructions": (
                f"Tell the user the email was sent to {to_address} "
                f"with subject '{subject}'."
            )
        })

    except smtplib.SMTPAuthenticationError:
        return core._wrap_tool_result("send_email", {
            "text": "Authentication failed. Check the SMTP username and password in config."
        })
    except smtplib.SMTPException as e:
        return core._wrap_tool_result("send_email", {
            "text": f"SMTP error: {str(e)}"
        })
    except Exception as e:
        return core._wrap_tool_result("send_email", {
            "text": f"Failed to send email: {str(e)}"
        })