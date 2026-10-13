"""Login: credentials (username or e-mail address) and the user behind a login session.

The admin from `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH`) always works as a fallback:
its row is created on its first login, has no password hash of its own and is always an
active admin, so a lost database password can never lock everybody out.
"""

from datetime import UTC, datetime
from functools import cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.emails import MAX_EMAIL_LENGTH
from flighttracker.models import User
from flighttracker.security.passwords import hash_password, verify_password
from flighttracker.services.users import ensure_env_admin, find_user, is_env_admin


@cache
def _dummy_hash() -> str:
    # Unknown usernames are checked against this, so response time does not reveal them.
    return hash_password("not-a-real-password")


def find_login(session: Session, login: str) -> User | None:
    """User by username, else by e-mail address (both ignoring case). A username never equals
    another user's address (see `_name_errors`), so the result is unambiguous."""
    login = login.strip()
    if not login:
        return None
    user = find_user(session, login)
    if user is None and "@" in login:
        user = session.scalar(select(User).where(func.lower(User.email) == login.lower()))
    return user


def authenticate(session: Session, settings: Settings, login: str, password: str) -> User | None:
    """The user for these credentials (username or e-mail address), or None. Always verifies
    one password hash."""
    login = login.strip()
    user = find_login(session, login) if len(login) <= MAX_EMAIL_LENGTH else None
    if login.lower() == settings.admin_username.lower() or (
        user is not None and is_env_admin(user, settings)
    ):
        if not verify_password(password, settings.admin_password_hash.get_secret_value()):
            return None
        user = ensure_env_admin(session, settings)
    else:
        usable = user is not None and user.is_active and user.password_hash is not None
        stored = user.password_hash if usable else _dummy_hash()
        if not verify_password(password, stored) or not usable:
            return None
    user.last_login_at = datetime.now(UTC)
    return user


def session_user(
    session: Session, settings: Settings, user_id: object, auth_version: object
) -> User | None:
    """The user of a login session, None when it was deactivated, changed or removed since."""
    if not isinstance(user_id, int) or not isinstance(auth_version, int):
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active or user.auth_version != auth_version:
        return None
    # A renamed `.env` admin leaves a row without password behind; it can no longer log in.
    if user.password_hash is None and not is_env_admin(user, settings):
        return None
    return user
