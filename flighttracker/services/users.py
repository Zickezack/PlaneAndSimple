"""Users: login, management by admins, personal settings.

The admin from `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH`) always works as a fallback:
its row is created on its first login, has no password hash of its own and is always an
active admin, so a lost database password can never lock everybody out.
"""

import re
from datetime import UTC, datetime
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session, aliased

from flighttracker.config import Settings
from flighttracker.i18n import SUPPORTED_LOCALES, Msg
from flighttracker.models import Search, SearchStatus, Share, Trip, TripLeg, User, UserRole
from flighttracker.security.passwords import hash_password, verify_password

MIN_PASSWORD_LENGTH = 12
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{2,60}$")


class UserValidationError(ValueError):
    def __init__(self, errors: list[Msg]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


@cache
def _dummy_hash() -> str:
    # Unknown usernames are checked against this, so response time does not reveal them.
    return hash_password("not-a-real-password")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_env_admin(user: User, settings: Settings) -> bool:
    return user.password_hash is None and user.username.lower() == settings.admin_username.lower()


def find_user(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(func.lower(User.username) == username.lower()))


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(func.lower(User.username))))


def _ensure_env_admin(session: Session, settings: Settings) -> User:
    user = find_user(session, settings.admin_username)
    if user is None:
        user = User(
            username=settings.admin_username,
            role=UserRole.ADMIN,
            is_active=True,
            auth_version=1,
        )
        session.add(user)
    # The env admin is always an active admin with its password in `.env`.
    user.password_hash = None
    user.role = UserRole.ADMIN
    user.is_active = True
    session.flush()
    # Suchabos and Trips from before user management belong to the env admin.
    for model in (Search, Trip):
        session.execute(update(model).where(model.owner_id.is_(None)).values(owner_id=user.id))
    return user


def authenticate(session: Session, settings: Settings, username: str, password: str) -> User | None:
    """The user for these credentials, or None. Always verifies one password hash."""
    username = username.strip()
    if username.lower() == settings.admin_username.lower():
        if not verify_password(password, settings.admin_password_hash.get_secret_value()):
            return None
        user = _ensure_env_admin(session, settings)
    else:
        user = find_user(session, username) if USERNAME_PATTERN.match(username) else None
        usable = user is not None and user.is_active and user.password_hash is not None
        stored = user.password_hash if usable else _dummy_hash()
        if not verify_password(password, stored) or not usable:
            return None
    user.last_login_at = _utcnow()
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


def _password_errors(password: str) -> list[Msg]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return [Msg("The password must be at least {n} characters long.", n=MIN_PASSWORD_LENGTH)]
    return []


def create_user(
    session: Session, settings: Settings, username: str, password: str, role: UserRole
) -> User:
    username = username.strip()
    errors: list[Msg] = []
    if not USERNAME_PATTERN.match(username):
        errors.append(Msg("Usernames have 2–60 characters: letters, digits and . _ @ - only."))
    elif username.lower() == settings.admin_username.lower() or find_user(session, username):
        errors.append(Msg('The username "{name}" is already taken.', name=username))
    errors.extend(_password_errors(password))
    if errors:
        raise UserValidationError(errors)
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        auth_version=1,
    )
    session.add(user)
    session.flush()
    return user


def _end_sessions(user: User) -> None:
    user.auth_version += 1


def set_password(user: User, password: str) -> None:
    errors = _password_errors(password)
    if errors:
        raise UserValidationError(errors)
    user.password_hash = hash_password(password)
    _end_sessions(user)


def change_own_password(user: User, current: str, new: str) -> None:
    if user.password_hash is None:
        raise UserValidationError([Msg("This account's password is set in .env.")])
    if not verify_password(current, user.password_hash):
        raise UserValidationError([Msg("The current password is wrong.")])
    set_password(user, new)


def _guard_admin_change(actor: User, user: User, settings: Settings) -> None:
    if user.id == actor.id:
        raise UserValidationError([Msg("You cannot change your own role, status or account.")])
    if is_env_admin(user, settings):
        raise UserValidationError([Msg("The admin from .env cannot be changed here.")])


def update_user(
    session: Session,
    actor: User,
    user: User,
    settings: Settings,
    *,
    role: UserRole,
    is_active: bool,
    max_searches: int | None,
    max_requests: int | None,
) -> int:
    """Admin edit of another user. Deactivating pauses the user's Suchabos and Trips unless
    another active user may edit them (see `pause_unattended`); returns how many were paused."""
    if role is not user.role or is_active != user.is_active:
        _guard_admin_change(actor, user, settings)
        _end_sessions(user)
    for value in (max_searches, max_requests):
        if value is not None and value < 0:
            raise UserValidationError([Msg("Limits cannot be negative.")])
    user.role = role
    user.max_searches = max_searches
    user.max_requests = max_requests
    user.is_active = is_active
    session.flush()
    return pause_unattended(session)


def pause_unattended(session: Session) -> int:
    """Pause active Suchabos and Trips of deactivated owners that no active user may edit.

    Shared with "edit", they keep running for the editors; once the last active editor is gone
    (share removed or reduced to "view", editor deactivated or deleted) they are paused, so
    nobody's provider budget is spent for nobody. Called after every such change.
    """
    owner, editor = aliased(User), aliased(User)
    paused = 0
    for model, shared in ((Search, Share.search_id), (Trip, Share.trip_id)):
        conditions = [
            model.status == SearchStatus.ACTIVE,
            model.archived_at.is_(None),
            exists().where(owner.id == model.owner_id, owner.is_active.is_(False)),
            ~exists().where(
                shared == model.id,
                Share.can_edit,
                editor.id == Share.user_id,
                editor.is_active,
            ),
        ]
        if model is Search:
            # Trip legs follow their Trip's status and sharing.
            conditions.append(~exists().where(TripLeg.search_id == Search.id))
        paused += session.execute(
            update(model).where(*conditions).values(status=SearchStatus.PAUSED)
        ).rowcount
    return paused


def delete_user(session: Session, actor: User, user: User, settings: Settings) -> int:
    """Their Suchabos and Trips go to the env admin – paused, unless another active user may
    edit them. Shares *with* the user are deleted (database cascade). Returns how many were
    paused."""
    _guard_admin_change(actor, user, settings)
    user.is_active = False
    session.flush()
    paused = pause_unattended(session)
    admin = _ensure_env_admin(session, settings)
    for model in (Search, Trip):
        session.execute(update(model).where(model.owner_id == user.id).values(owner_id=admin.id))
    session.delete(user)
    session.flush()
    return paused


def set_preferences(user: User, *, locale: str | None, timezone: str | None) -> None:
    errors: list[Msg] = []
    if locale is not None and locale not in SUPPORTED_LOCALES:
        errors.append(Msg("Unknown language."))
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(Msg('Unknown time zone "{zone}".', zone=timezone))
    if errors:
        raise UserValidationError(errors)
    user.locale = locale
    user.timezone = timezone or None


def usernames(session: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
    return {user_id: name for user_id, name in rows}
