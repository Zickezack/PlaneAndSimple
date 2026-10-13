"""One-time links to set a password: "Forgot password?" and the welcome mail of a new user.

The link carries a random token; only its SHA-256 is stored, so a database dump cannot be used
to log in. A token works once and expires; issuing a new one voids the user's older ones, and
setting a password voids all of them.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import PasswordToken, TokenPurpose, User
from flighttracker.services.users import is_env_admin, set_password

VALIDITY = {TokenPurpose.RESET: timedelta(hours=1), TokenPurpose.WELCOME: timedelta(days=7)}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def can_reset(user: User | None, settings: Settings) -> bool:
    """Only active database users with an address; the `.env` admin's password lives in `.env`."""
    return bool(user and user.is_active and user.email and not is_env_admin(user, settings))


def _void_open_tokens(session: Session, user: User, now: datetime) -> None:
    session.execute(
        update(PasswordToken)
        .where(PasswordToken.user_id == user.id, PasswordToken.used_at.is_(None))
        .values(used_at=now)
    )


def issue(session: Session, user: User, purpose: TokenPurpose, now: datetime) -> str:
    """A new token for the link; older open ones of the user stop working."""
    _void_open_tokens(session, user, now)
    token = secrets.token_urlsafe(32)
    session.add(
        PasswordToken(
            user_id=user.id,
            token_hash=_hash(token),
            purpose=purpose,
            expires_at=now + VALIDITY[purpose],
        )
    )
    session.flush()
    return token


def find_user(session: Session, settings: Settings, token: str, now: datetime) -> User | None:
    """The user of a valid, unused, unexpired token (and still allowed to reset)."""
    row = session.scalar(
        select(PasswordToken).where(
            PasswordToken.token_hash == _hash(token),
            PasswordToken.used_at.is_(None),
            PasswordToken.expires_at > now,
        )
    )
    if row is None:
        return None
    user = row.user
    return user if user.is_active and not is_env_admin(user, settings) else None


def redeem(
    session: Session, settings: Settings, token: str, password: str, now: datetime
) -> User | None:
    """Sets the password (UserValidationError if too weak) and voids every open token of the
    user. None if the token is not valid (any more)."""
    user = find_user(session, settings, token, now)
    if user is None:
        return None
    set_password(user, password)
    _void_open_tokens(session, user, now)
    return user
