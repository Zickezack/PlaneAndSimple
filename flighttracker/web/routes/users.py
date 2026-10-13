"""User management (admins only): create users, roles, deactivate, limits, reset passwords."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.models import User, UserRole
from flighttracker.services import quota
from flighttracker.services.mail import mail_enabled
from flighttracker.services.users import (
    UserValidationError,
    create_user,
    delete_user,
    is_env_admin,
    list_users,
    rename_user,
    set_email,
    set_password,
    update_user,
)
from flighttracker.web.deps import (
    csrf_form,
    flash,
    get_db,
    get_effective_settings,
    get_user,
    require_admin,
)
from flighttracker.web.mails import send_password_changed, send_welcome
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _redirect(url: str = "/users") -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _flash_errors(request: Request, exc: UserValidationError) -> None:
    for error in exc.errors:
        flash(request, error, "error")


def _flash_paused(request: Request, paused: int) -> None:
    flash(
        request,
        Msg(
            "{n} tracked searches and Trips were paused: no active user may edit them any more.",
            n=paused,
        ),
    )


def _role(raw: object) -> UserRole:
    return UserRole.ADMIN if raw == UserRole.ADMIN.value else UserRole.USER


def _optional_limit(raw: object) -> int | None:
    """Empty = the platform default; anything that is not a whole number is rejected."""
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.isascii() or not text.isdigit():
        raise UserValidationError([Msg("Limits must be whole numbers.")])
    return int(text)


def _load(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.get("/users")
def user_list(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    users = list_users(db)
    return templates.TemplateResponse(
        request,
        "users/list.html",
        {
            "users": users,
            "usage": {user.id: quota.usage(db, user, settings) for user in users},
            "env_admin": {user.id for user in users if is_env_admin(user, settings)},
            "mail_enabled": mail_enabled(settings),
        },
    )


@router.post("/users")
def create(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    password, email = str(form.get("password", "")), str(form.get("email", "")).strip()
    if not password and email and not mail_enabled(settings):
        flash(request, Msg("Mail is not set up: enter an initial password."), "error")
        return _redirect()
    try:
        user = create_user(
            db,
            settings,
            str(form.get("username", "")),
            password,
            _role(form.get("role")),
            email=email,
        )
    except UserValidationError as exc:
        _flash_errors(request, exc)
        return _redirect()
    db.commit()
    flash(request, Msg('User "{name}" created.', name=user.username), "success")
    if user.email and mail_enabled(settings):
        if send_welcome(request, db, settings, user):
            flash(request, Msg("Welcome mail sent to {email}.", email=user.email), "success")
        else:
            flash(request, Msg("The welcome mail could not be sent (see the mail log)."), "error")
    return _redirect()


@router.get("/users/{user_id}")
def edit_form(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    actor: User = Depends(get_user),
):
    user = _load(db, user_id)
    return templates.TemplateResponse(
        request,
        "users/edit.html",
        {
            "user": user,
            "usage": quota.usage(db, user, settings),
            "is_env_admin": is_env_admin(user, settings),
            "is_self": user.id == actor.id,
            "mail_enabled": mail_enabled(settings),
            "defaults": (settings.max_searches_per_user, settings.max_requests_per_user),
        },
    )


@router.post("/users/{user_id}")
def edit(
    user_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    actor: User = Depends(get_user),
):
    user = _load(db, user_id)
    try:
        paused = update_user(
            db,
            actor,
            user,
            settings,
            role=_role(form.get("role")),
            is_active=form.get("is_active") == "1",
            max_searches=_optional_limit(form.get("max_searches")),
            max_requests=_optional_limit(form.get("max_requests")),
        )
        rename_user(db, user, settings, str(form.get("username", user.username)))
        set_email(db, user, str(form.get("email", "")))
    except UserValidationError as exc:
        db.rollback()
        _flash_errors(request, exc)
        return _redirect(f"/users/{user_id}")
    db.commit()
    flash(request, Msg("User saved."), "success")
    if paused:
        _flash_paused(request, paused)
    return _redirect(f"/users/{user_id}")


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    user = _load(db, user_id)
    if is_env_admin(user, settings):
        flash(request, Msg("This account's password is set in .env."), "error")
        return _redirect(f"/users/{user_id}")
    try:
        set_password(user, str(form.get("password", "")))
    except UserValidationError as exc:
        _flash_errors(request, exc)
        return _redirect(f"/users/{user_id}")
    db.commit()
    send_password_changed(request, db, settings, user)
    flash(request, Msg("Password set. The user's sessions were logged out."), "success")
    return _redirect(f"/users/{user_id}")


@router.post("/users/{user_id}/welcome")
def resend_welcome(
    user_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
):
    """The welcome mail again, e.g. after its link expired; a new link replaces older ones."""
    user = _load(db, user_id)
    if is_env_admin(user, settings) or not user.email or not mail_enabled(settings):
        flash(request, Msg("A welcome mail needs mail set up and an e-mail address."), "error")
    elif send_welcome(request, db, settings, user):
        flash(request, Msg("Welcome mail sent to {email}.", email=user.email), "success")
    else:
        flash(request, Msg("The welcome mail could not be sent (see the mail log)."), "error")
    return _redirect(f"/users/{user_id}")


@router.post("/users/{user_id}/delete")
def delete(
    user_id: int,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_effective_settings),
    actor: User = Depends(get_user),
):
    user = _load(db, user_id)
    if str(form.get("confirm_name", "")).strip() != user.username:
        flash(request, Msg("To delete permanently, please enter the exact name."), "error")
        return _redirect(f"/users/{user_id}")
    name = user.username
    try:
        paused = delete_user(db, actor, user, settings)
    except UserValidationError as exc:
        db.rollback()
        _flash_errors(request, exc)
        return _redirect(f"/users/{user_id}")
    db.commit()
    flash(
        request,
        Msg(
            'User "{name}" deleted. Their tracked searches and Trips now belong to the admin.',
            name=name,
        ),
        "success",
    )
    if paused:
        _flash_paused(request, paused)
    return _redirect()
