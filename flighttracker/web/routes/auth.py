import hmac
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.security.passwords import verify_password
from flighttracker.web.deps import (
    SESSION_CSRF,
    SESSION_USER,
    csrf_form,
    current_user,
    get_settings,
)
from flighttracker.web.templating import templates

router = APIRouter()


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    # Always verify the password, so response time does not reveal valid usernames.
    user_ok = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    password_ok = verify_password(password, settings.admin_password_hash.get_secret_value())
    return user_ok and password_ok


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse("/searches", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    form: FormData = Depends(csrf_form),
    settings: Settings = Depends(get_settings),
) -> Response:
    throttle = request.app.state.login_throttle
    key, now = _client_key(request), datetime.now(UTC)
    if not throttle.try_attempt(key, now):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": Msg("Too many failed attempts. Please try again in a few minutes.")},
            status_code=429,
        )
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not check_credentials(settings, username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": Msg("Wrong username or password.")},
            status_code=401,
        )
    throttle.reset(key)
    # New session on login (prevents session fixation).
    request.session.clear()
    request.session[SESSION_USER] = settings.admin_username
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)
    return RedirectResponse("/searches", status_code=303)


@router.post("/logout")
def logout(request: Request, form: FormData = Depends(csrf_form)) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
