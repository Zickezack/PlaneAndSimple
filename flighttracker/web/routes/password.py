"""Passwords by mail: "Forgot password?" (one-time link to set a new one), the page behind the
link (also used by the welcome mail), and the admin's test mail. Only with mail set up."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.models import TokenPurpose, User
from flighttracker.services import mail, mail_messages, password_tokens
from flighttracker.services.users import UserValidationError
from flighttracker.web.deps import (
    csrf_form,
    flash,
    get_db,
    get_effective_settings,
    get_user,
    require_admin,
)
from flighttracker.web.mails import mail_link, send_password_changed
from flighttracker.web.templating import templates

router = APIRouter()

RESET_MINUTES = int(password_tokens.VALIDITY[TokenPurpose.RESET].total_seconds() // 60)


@router.get("/password/forgot")
def forgot_form(request: Request, settings: Settings = Depends(get_effective_settings)):
    return templates.TemplateResponse(
        request, "password/forgot.html", {"mail_enabled": mail.mail_enabled(settings)}
    )


@router.post("/password/forgot")
def forgot(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
) -> Response:
    now = datetime.now(UTC)
    client = request.client.host if request.client else "unknown"
    # Same limit as failed logins: nobody can flood an address with reset mails.
    if not request.app.state.login_throttle.try_attempt(f"reset:{client}", now):
        flash(request, Msg("Too many attempts. Please try again in a few minutes."), "error")
        return RedirectResponse("/password/forgot", status_code=303)
    user = password_tokens.find_by_login(db, str(form.get("login", "")))
    if mail.mail_enabled(settings) and password_tokens.can_reset(user, settings):
        token = password_tokens.issue(db, user, TokenPurpose.RESET, now)
        db.commit()
        link = mail_link(request, settings, f"/password/reset/{token}")
        mail.send_logged(settings, mail_messages.reset_mail(user, link, RESET_MINUTES))
    # The same answer for every input: it must not reveal which accounts exist.
    flash(
        request,
        Msg(
            "If an account with an e-mail address matches, "
            "a link to set a new password is on its way."
        ),
        "success",
    )
    return RedirectResponse("/login", status_code=303)


def _reset_page(request: Request, token: str, errors: list[Msg], valid: bool, status: int = 200):
    return templates.TemplateResponse(
        request,
        "password/reset.html",
        {"token": token, "errors": errors, "valid": valid},
        status_code=status,
    )


@router.get("/password/reset/{token}")
def reset_form(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    valid = password_tokens.find_user(db, settings, token, datetime.now(UTC)) is not None
    return _reset_page(request, token, [], valid, 200 if valid else 404)


@router.post("/password/reset/{token}")
def reset(
    token: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    password = str(form.get("new_password", ""))
    if password != str(form.get("repeat_password", "")):
        return _reset_page(request, token, [Msg("The new passwords do not match.")], True, 400)
    try:
        user = password_tokens.redeem(db, settings, token, password, datetime.now(UTC))
    except UserValidationError as exc:
        db.rollback()
        return _reset_page(request, token, exc.errors, True, 400)
    if user is None:
        return _reset_page(request, token, [], False, 404)
    db.commit()
    send_password_changed(request, settings, user)
    flash(request, Msg("Password set. You can log in now."), "success")
    return RedirectResponse("/login", status_code=303)


@router.post("/settings/mail/test", dependencies=[Depends(require_admin)])
def test_mail(
    request: Request,
    form: FormData = Depends(csrf_form),
    settings: Settings = Depends(get_effective_settings),
    user: User = Depends(get_user),
):
    target = "/settings#mail"
    if not user.email:
        flash(request, Msg("Add your e-mail address under Personal settings first."), "error")
        return RedirectResponse(target, status_code=303)
    try:
        mail.send(settings, mail_messages.test_mail(user.email, mail_messages.locale_of(user)))
    except mail.MailError as exc:
        flash(request, Msg("Test mail not sent: {error}", error=str(exc)), "error")
        return RedirectResponse(target, status_code=303)
    flash(request, Msg("Test mail sent to {email}.", email=user.email), "success")
    return RedirectResponse(target, status_code=303)
