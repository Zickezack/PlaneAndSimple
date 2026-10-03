import hmac
import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, Msg
from flighttracker.services.settings import effective_settings

SESSION_USER = "user"
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


def current_user(request: Request) -> str | None:
    user = request.session.get(SESSION_USER)
    # Sessions of a renamed/removed admin become invalid automatically.
    if user is None or user != request.app.state.settings.admin_username:
        return None
    return user


def require_admin(request: Request) -> str:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


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
