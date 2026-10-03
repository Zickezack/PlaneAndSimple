import hmac
import secrets
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, Msg
from flighttracker.models import User
from flighttracker.services.settings import effective_settings
from flighttracker.services.users import session_user

SESSION_USER = "user_id"
SESSION_AUTH_VERSION = "auth_version"
SESSION_CSRF = "csrf_token"
SESSION_FLASHES = "flashes"
SESSION_LOCALE = "locale"


class LoginRequired(Exception):
    pass


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_effective_settings(request: Request, db: Session = Depends(get_db)) -> Settings:
    """`Settings` with Platform Settings overrides applied (flight provider, worker, …)."""
    return effective_settings(db, request.app.state.settings)


@dataclass(frozen=True)
class AuthUser:
    """The logged-in user as the web layer sees it (detached from any DB session)."""

    id: int
    username: str
    is_admin: bool
    can_change_password: bool
    timezone: str | None


def current_user(request: Request) -> AuthUser | None:
    """Loaded once per request; None when not logged in or the account changed since."""
    if hasattr(request.state, "auth_user"):
        return request.state.auth_user
    auth_user = None
    user_id = request.session.get(SESSION_USER)
    if user_id is not None:
        with request.app.state.session_factory() as session:
            user = session_user(
                session,
                request.app.state.settings,
                user_id,
                request.session.get(SESSION_AUTH_VERSION),
            )
            if user is not None:
                auth_user = AuthUser(
                    user.id,
                    user.username,
                    user.is_admin,
                    user.password_hash is not None,
                    user.timezone,
                )
    request.state.auth_user = auth_user
    return auth_user


def start_session(request: Request, user: User) -> None:
    # New session on login (prevents session fixation).
    request.session.clear()
    request.session[SESSION_USER] = user.id
    request.session[SESSION_AUTH_VERSION] = user.auth_version
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)
    if user.locale in SUPPORTED_LOCALES:
        request.session[SESSION_LOCALE] = user.locale
    # Pages rendered later in this request must see the new login, not a cached "nobody".
    if hasattr(request.state, "auth_user"):
        del request.state.auth_user


def require_login(request: Request) -> AuthUser:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: AuthUser = Depends(require_login)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can open this page.")
    return user


def get_user(user: AuthUser = Depends(require_login), db: Session = Depends(get_db)) -> User:
    """The logged-in user as a row of the request's DB session (for changes and services)."""
    return db.get(User, user.id)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


async def csrf_form(request: Request) -> FormData:
    """Parsed form body of a POST, rejected unless it carries the session's CSRF token."""
    form = await request.form()
    expected = request.session.get(SESSION_CSRF)
    submitted = form.get(SESSION_CSRF)
    if (
        not expected
        or not isinstance(submitted, str)
        or not hmac.compare_digest(submitted, expected)
    ):
        raise HTTPException(status_code=403, detail="Invalid form token. Please reload the page.")
    return form


def current_locale(request: Request) -> str:
    locale = request.session.get(SESSION_LOCALE)
    return locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE


def flash(request: Request, message: Msg, category: str = "info") -> None:
    flashes = request.session.get(SESSION_FLASHES, [])
    entry = {"message": message.to_json(), "category": category}
    request.session[SESSION_FLASHES] = [*flashes, entry]


def pop_flashes(request: Request) -> list[dict]:
    return [
        {"message": Msg.from_json(f["message"]), "category": f["category"]}
        for f in request.session.pop(SESSION_FLASHES, [])
    ]
