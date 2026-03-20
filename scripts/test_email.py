#!/usr/bin/env python3
"""
Test email sending using the send_email tool config.

Usage:
    python3 scripts/test_email.py
    python3 scripts/test_email.py --to Jesse
    python3 scripts/test_email.py --to jesse@example.com
"""

import sys
import os
import argparse
import yaml
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

CONFIG_PATH = os.path.join(os.path.dirname(__file__), '../config/send_email.yaml')


def resolve_address(name_or_email: str, config: dict) -> str:
    if not name_or_email:
        return ''
    contacts = config.get('contacts', {})
    for name, email in contacts.items():
        if name.lower() == name_or_email.lower():
            return email
    if '@' in name_or_email:
        return name_or_email
    return ''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Send a test email using send_email config")
    parser.add_argument('--to', metavar='NAME_OR_EMAIL', default='',
                        help="Recipient name from contacts or raw email address")
    args = parser.parse_args()

    if not os.path.exists(CONFIG_PATH):
        print(f"Config not found at {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    smtp_host  = config.get('smtp_host', 'smtp.gmail.com')
    smtp_port  = config.get('smtp_port', 587)
    username   = config.get('username', '')
    password   = config.get('password', '')
    from_addr  = config.get('from_address', username)
    default_to = config.get('default_to', '')
    contacts   = config.get('contacts', {})

    print(f"Config loaded from {CONFIG_PATH}")
    print(f"  SMTP host:    {smtp_host}:{smtp_port}")
    print(f"  From:         {from_addr}")
    print(f"  Auth:         {'yes' if username else 'no'}")
    print(f"  Contacts:     {list(contacts.keys()) or '(none)'}")

    # Resolve recipient
    to_address = resolve_address(args.to, config) if args.to else ''
    if not to_address:
        to_address = resolve_address(default_to, config) or default_to
    if not to_address:
        print("\nNo recipient specified and no default_to set.")
        print("Use --to <name or email>, or set default_to in the config.")
        sys.exit(1)

    print(f"\nSending test email to: {to_address}")

    try:
        msg = MIMEMultipart()
        msg['From']    = from_addr
        msg['To']      = to_address
        msg['Subject'] = "Supernova test email"
        msg.attach(MIMEText(
            "This is a test email from Supernova.\n\nIf you received this, email sending is working correctly.",
            'plain'
        ))

        if smtp_port == 465:
            server_cls = smtplib.SMTP_SSL
            with server_cls(smtp_host, smtp_port) as server:
                if username and password:
                    server.login(username, password)
                server.sendmail(from_addr, to_address, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if username and password:
                    server.starttls()
                    server.login(username, password)
                server.sendmail(from_addr, to_address, msg.as_string())

        print(f"✓ Test email sent successfully to {to_address}")

    except smtplib.SMTPAuthenticationError:
        print("✗ Authentication failed — check username and password in config")
        sys.exit(1)
    except smtplib.SMTPException as e:
        print(f"✗ SMTP error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Failed: {e}")
        sys.exit(1)