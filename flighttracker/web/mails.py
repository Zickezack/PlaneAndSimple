"""Account mails sent from web requests (links point to this server's public address). Each
helper commits its mail log entry (and token) – it runs after the request's own commit."""

from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import TokenPurpose, User
from flighttracker.services import mail, mail_messages, password_tokens

WELCOME_DAYS = password_tokens.VALIDITY[TokenPurpose.WELCOME].days


def mail_link(request: Request, settings: Settings, path: str) -> str | None:
    return mail.link(settings, path, base_url=str(request.base_url))


def send_password_changed(request: Request, db: Session, settings: Settings, user: User) -> None:
    """Notice to the user's address after any password change (nobody waits for it)."""
    if user.email and mail.mail_enabled(settings):
        reset = mail_link(request, settings, "/password/forgot")
        mail.deliver(db, settings, mail_messages.password_changed_mail(user, reset))
        db.commit()


def send_welcome(request: Request, db: Session, settings: Settings, user: User) -> bool:
    """Welcome mail to a user with an address – on creation and again on the admin's request;
    without a password yet it carries a new one-time link to set one (older links stop
    working). False if it could not be sent."""
    if not (user.email and mail.mail_enabled(settings)):
        return False
    password_link = None
    if user.password_hash is None:
        token = password_tokens.issue(db, user, TokenPurpose.WELCOME, datetime.now(UTC))
        db.commit()
        password_link = mail_link(request, settings, f"/password/reset/{token}")
    message = mail_messages.welcome_mail(
        user, mail_link(request, settings, "/login"), password_link, WELCOME_DAYS
    )
    sent = mail.deliver(db, settings, message)
    db.commit()
    return sent
