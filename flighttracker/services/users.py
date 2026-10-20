"""Users: management by admins (create, rename, roles, limits, delete), passwords, personal
settings. Logging in is `services/login.py`; the `.env` admin is explained there."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session, aliased

from flighttracker.config import Settings
from flighttracker.domain.emails import is_valid_email
from flighttracker.i18n import SUPPORTED_LOCALES, Msg
from flighttracker.models import (
    DigestFrequency,
    Search,
    SearchStatus,
    Share,
    Trip,
    TripLeg,
    User,
    UserRole,
)
from flighttracker.security.passwords import hash_password, verify_password

MIN_PASSWORD_LENGTH = 12
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{2,60}$")


class UserValidationError(ValueError):
    def __init__(self, errors: list[Msg]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


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


def _password_errors(password: str) -> list[Msg]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return [Msg("The password must be at least {n} characters long.", n=MIN_PASSWORD_LENGTH)]
    return []


def ensure_env_admin(session: Session, settings: Settings) -> User:
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


def _taken_by_other(session: Session, value: str, user_id: int | None) -> bool:
    """Whether another user has this as username or e-mail address (ignoring case) – both
    identify a user at login, so they must be unique across each other."""
    value = value.lower()
    return (
        session.scalar(
            select(User.id).where(
                (func.lower(User.username) == value) | (func.lower(User.email) == value),
                User.id != (user_id or 0),
            )
        )
        is not None
    )


def _name_errors(
    session: Session, settings: Settings, username: str, user_id: int | None = None
) -> list[Msg]:
    if not USERNAME_PATTERN.match(username):
        return [Msg("Usernames have 2–60 characters: letters, digits and . _ @ - only.")]
    if username.lower() == settings.admin_username.lower() or _taken_by_other(
        session, username, user_id
    ):
        return [Msg('The username "{name}" is already taken.', name=username)]
    return []


def _email_errors(session: Session, email: str, user_id: int | None = None) -> list[Msg]:
    if not is_valid_email(email):
        return [Msg('"{email}" is not a valid e-mail address.', email=email)]
    if _taken_by_other(session, email, user_id):
        return [Msg("This e-mail address is already used by another user.")]
    return []


def rename_user(session: Session, user: User, settings: Settings, username: str) -> None:
    """Admins rename any database user; ownership, shares and alerts follow (they refer to the
    id). The `.env` admin's name comes from `ADMIN_USERNAME`."""
    username = username.strip()
    if username == user.username:
        return
    if is_env_admin(user, settings):
        raise UserValidationError([Msg("The admin from .env cannot be changed here.")])
    errors = _name_errors(session, settings, username, user.id)
    if errors:
        raise UserValidationError(errors)
    user.username = username


def set_email(session: Session, user: User, raw: str) -> None:
    """Optional address for mails; empty removes it (no mails for this user then)."""
    email = raw.strip() or None
    errors = _email_errors(session, email, user.id) if email else []
    if errors:
        raise UserValidationError(errors)
    user.email = email


def create_user(
    session: Session,
    settings: Settings,
    username: str,
    password: str,
    role: UserRole,
    *,
    email: str = "",
) -> User:
    """`password` may stay empty when an `email` is given: the user then sets it through the
    link of the welcome mail and cannot log in before."""
    username, email = username.strip(), email.strip()
    errors = _name_errors(session, settings, username)
    if email:
        errors.extend(_email_errors(session, email))
    if password or not email:
        errors.extend(_password_errors(password))
    if errors:
        raise UserValidationError(errors)
    user = User(
        username=username,
        password_hash=hash_password(password) if password else None,
        email=email or None,
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
    admin = ensure_env_admin(session, settings)
    for model in (Search, Trip):
        session.execute(update(model).where(model.owner_id == user.id).values(owner_id=admin.id))
    session.delete(user)
    session.flush()
    return paused


def set_preferences(
    user: User,
    *,
    locale: str | None,
    timezone: str | None,
    deal_mails: bool | None = None,
    digest: str | None = None,
    now: datetime | None = None,
) -> None:
    """`deal_mails`, `digest`: None keeps the current choice. A digest switched on starts with
    the next period (`now`), not with one right away."""
    errors: list[Msg] = []
    if digest is not None and digest not in {f.value for f in DigestFrequency}:
        errors.append(Msg("Choose how often the price digest comes."))
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
    if deal_mails is not None:
        user.deal_mails = deal_mails
    if digest is not None:
        chosen = DigestFrequency(digest)
        if user.digest is DigestFrequency.OFF and chosen is not DigestFrequency.OFF:
            user.digest_at = now or datetime.now(UTC)
        user.digest = chosen


def usernames(session: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
    return {user_id: name for user_id, name in rows}
