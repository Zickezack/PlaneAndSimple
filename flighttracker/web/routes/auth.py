from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.services.login import authenticate
from flighttracker.services.mail import mail_enabled
from flighttracker.services.settings import effective_settings
from flighttracker.web.deps import csrf_form, current_user, get_db, get_settings, start_session
from flighttracker.web.templating import templates

router = APIRouter()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_page(request: Request, db: Session, error: Msg | None, status_code: int = 200):
    settings = effective_settings(db, request.app.state.settings)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "mail_enabled": mail_enabled(settings)},
        status_code=status_code,
    )


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)) -> Response:
    if current_user(request):
        return RedirectResponse("/searches", status_code=303)
    return _login_page(request, db, None)


@router.post("/login")
def login(
    request: Request,
    form: FormData = Depends(csrf_form),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Response:
    throttle = request.app.state.login_throttle
    key, now = _client_key(request), datetime.now(UTC)
    if not throttle.try_attempt(key, now):
        return _login_page(
            request, db, Msg("Too many failed attempts. Please try again in a few minutes."), 429
        )
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    user = authenticate(db, settings, username, password)
    if user is None:
        return _login_page(request, db, Msg("Wrong username or password."), 401)
    db.commit()
    throttle.reset(key)
    start_session(request, user)
    return RedirectResponse("/searches", status_code=303)


@router.post("/logout")
def logout(request: Request, form: FormData = Depends(csrf_form)) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
