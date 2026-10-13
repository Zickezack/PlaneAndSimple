"""Sending mails over SMTP (Platform Settings → Mail, or `.env`).

Every mail has a plain-text part and an HTML part made from the same text (paragraphs, bullet
lists, clickable links). Sent synchronously: account mails from the web request that triggers
them, price alerts from the worker after a poll. `deliver` logs every attempt (`mail_log`, for
admins). Without host and sender, mail is "off": the mail features are hidden.
"""

import html
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import MailLog

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
SENDER_NAME = "Plane and simple"


# The mail log keeps half a year.
LOG_RETENTION = timedelta(days=183)
_URL = re.compile(r"https?://[^\s<>\"]+")


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str
    kind: str = "other"  # for the mail log: welcome, reset, password_changed, price_alert, test
    user_id: int | None = None


class MailError(Exception):
    """Sending failed; the message names the cause but never a credential."""


def mail_enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.mail_from)


def link(settings: Settings, path: str, base_url: str | None = None) -> str | None:
    """Absolute link for a mail: the public address from the settings, else `base_url` (the
    address of the current web request); None in the worker without a public address."""
    base = settings.public_base_url or base_url
    return f"{base.rstrip('/')}{path}" if base else None


def _inline(text: str) -> str:
    """Escaped text with clickable links."""
    parts, last = [], 0
    for match in _URL.finditer(text):
        url = match.group(0)
        parts += [html.escape(text[last : match.start()])]
        parts += [f'<a href="{html.escape(url)}" style="color:#1f5fbf">{html.escape(url)}</a>']
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "<br>".join("".join(parts).split("\n"))


def render_html(subject: str, body: str) -> str:
    """HTML part of a mail from its text: a paragraph per block, "• " lines as a list."""
    blocks = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        if all(line.startswith("• ") for line in lines):
            items = "".join(f"<li>{_inline(line[2:])}</li>" for line in lines)
            blocks.append(f'<ul style="padding-left:1.2em;margin:0 0 1em">{items}</ul>')
        else:
            blocks.append(f'<p style="margin:0 0 1em">{_inline(block)}</p>')
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f5f6f8;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c2430">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #d8dee6;'
        'border-radius:8px;padding:24px;line-height:1.5">'
        f'<p style="margin:0 0 1em;font-weight:700">✈ {html.escape(SENDER_NAME)}</p>'
        f'<h1 style="font-size:18px;margin:0 0 1em">{html.escape(subject)}</h1>'
        f"{''.join(blocks)}</div></body></html>"
    )


def _message(settings: Settings, mail: Mail) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((SENDER_NAME, settings.mail_from))
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message["Message-ID"] = make_msgid(domain=settings.mail_from.rpartition("@")[2] or None)
    message.set_content(mail.body)
    message.add_alternative(render_html(mail.subject, mail.body), subtype="html")
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


def deliver(
    session: Session, settings: Settings, mail: Mail, *, raise_errors: bool = False
) -> bool:
    """Send and log the attempt (`mail_log`; the caller commits). A failure is logged and
    returns False – or raises `MailError` with `raise_errors` (e.g. for the test mail)."""
    error = None
    try:
        send(settings, mail)
    except MailError as exc:
        error = str(exc)
        log.warning("Mail not sent (%s): %s", mail.subject, exc)
    session.add(
        MailLog(
            user_id=mail.user_id,
            kind=mail.kind,
            subject=mail.subject[:200],
            ok=error is None,
            error=error,
        )
    )
    session.execute(delete(MailLog).where(MailLog.sent_at < datetime.now(UTC) - LOG_RETENTION))
    session.flush()
    if error and raise_errors:
        raise MailError(error)
    return error is None
