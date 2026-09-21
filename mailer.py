"""Outgoing email for GoGather, using only the standard library (smtplib).

Configured entirely through environment variables (see .env.example):
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY.

When SMTP_HOST isn't set, nothing is sent -- the message is written to the log
instead, so local development works without a mail server. In development the
log line includes the message body (handy for reading a password-reset code);
with APP_ENV=production it doesn't, so codes never end up in log files.
"""

import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

log = logging.getLogger("gogather.mail")


def is_configured():
    return bool(os.environ.get("SMTP_HOST"))


def _deliver(message):
    host = os.environ["SMTP_HOST"]
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    port = int(os.environ.get("SMTP_PORT", 465 if security == "ssl" else 587))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        with server:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password or "")
            server.send_message(message)
        log.info("Email sent to %s: %s", message["To"], message["Subject"])
    except Exception:
        # An email failing must never break the booking/payment that
        # triggered it -- record it for whoever runs the server and move on.
        log.exception("Could not send email to %s: %s", message["To"], message["Subject"])


def send_email(to_address, subject, body):
    """Queue a plain-text email. Returns True if it was handed to the SMTP
    sender, False if it was skipped (no SMTP configured, or no address).
    Delivery happens in a background thread so a slow mail server never
    delays the page the traveller is waiting on."""
    if not to_address:
        return False

    if not is_configured():
        if os.environ.get("APP_ENV", "development").lower() == "production":
            log.warning("SMTP is not configured -- email to %s not sent: %s", to_address, subject)
        else:
            log.info("SMTP not configured -- email NOT sent.\nTo: %s\nSubject: %s\n\n%s", to_address, subject, body)
        return False

    message = EmailMessage()
    message["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "no-reply@gogather.local"
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    threading.Thread(target=_deliver, args=(message,), daemon=True).start()
    return True
