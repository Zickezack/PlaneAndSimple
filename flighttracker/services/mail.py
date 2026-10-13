"""Sending mails over SMTP (Platform Settings → Mail, or `.env`).

Plain-text mails, sent synchronously: account mails from the web request that triggers them,
price alerts from the worker after a poll. Without host and sender, mail is "off": the mail
features are hidden and nothing is sent.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from flighttracker.config import Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
SENDER_NAME = "Plane and simple"


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str


class MailError(Exception):
    """Sending failed; the message names the cause but never a credential."""


def mail_enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.mail_from)


def link(settings: Settings, path: str, base_url: str | None = None) -> str | None:
    """Absolute link for a mail: the public address from the settings, else `base_url` (the
    address of the current web request); None in the worker without a public address."""
    base = settings.public_base_url or base_url
    return f"{base.rstrip('/')}{path}" if base else None


def _message(settings: Settings, mail: Mail) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((SENDER_NAME, settings.mail_from))
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message["Message-ID"] = make_msgid(domain=settings.mail_from.rpartition("@")[2] or None)
    message.set_content(mail.body)
    return message


def send(settings: Settings, mail: Mail) -> None:
    if not mail_enabled(settings):
        raise MailError("Mail is not set up (Platform Settings → Mail).")
    host, port = settings.smtp_host, settings.smtp_port
    try:
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(
                host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
        with server:
            if settings.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                password = (
                    settings.smtp_password.get_secret_value() if settings.smtp_password else ""
                )
                server.login(settings.smtp_username, password)
            server.send_message(_message(settings, mail))
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"The mail server rejected the login (SMTP {exc.smtp_code}).") from None
    except smtplib.SMTPException as exc:
        raise MailError(f"The mail server refused the mail ({type(exc).__name__}).") from None
    except OSError as exc:
        raise MailError(f"No connection to {host}:{port} ({type(exc).__name__}).") from None
    log.info("Mail sent: %s", mail.subject)


def send_logged(settings: Settings, mail: Mail) -> bool:
    """Send; a failure is logged, not raised. For mails nobody waits for on screen."""
    try:
        send(settings, mail)
    except MailError as exc:
        log.warning("Mail not sent (%s): %s", mail.subject, exc)
        return False
    return True
