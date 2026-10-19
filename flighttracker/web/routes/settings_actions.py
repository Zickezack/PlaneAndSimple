"""Actions of a Platform Settings section (admins only): save it, check the mail login, send a
test mail, test the AI connection.

The tests use the values as entered – nothing has to be saved first. With JavaScript
(static/js/settings-sections.js) every action answers with JSON and the page stays where it is;
without it a failed save or a test shows the page again with everything that was typed.
"""

from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.ai_providers import OLLAMA
from flighttracker.i18n import Msg, translate
from flighttracker.models import User
from flighttracker.services import ai, ai_prompts, mail, mail_messages, poll_plan
from flighttracker.services.settings import AI_SECTION, MAIL_SECTION, effective_settings
from flighttracker.web import labels
from flighttracker.web.ai_report import ollama_report
from flighttracker.web.deps import (
    csrf_form,
    current_locale,
    flash,
    get_db,
    get_user,
    require_admin,
)
from flighttracker.web.routes.settings import render_settings
from flighttracker.web.settings_forms import SECTIONS, section_fields, setup_status
from flighttracker.web.settings_sections import (
    SectionInput,
    draft_settings,
    read_section,
    store_section,
)
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

Results = list[tuple[Msg, str]]
FIX_FIELDS = Msg("Nothing saved or tested – please correct the marked fields.")


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _respond(
    request: Request, db: Session, user: User, entered: SectionInput, results: Results, saved=False
) -> Response:
    """JSON for the script; without it a redirect after a save, else the page again."""
    if _wants_json(request):
        locale = current_locale(request)
        settings = effective_settings(db, request.app.state.settings)
        setup = templates.TemplateResponse(
            request, "settings/_setup.html", {"setup": setup_status(settings)}
        )
        return JSONResponse(
            {
                "ok": entered.ok,
                "messages": [
                    {"text": message.render(locale), "category": category}
                    for message, category in results
                ],
                "errors": {
                    key: f"{translate(labels.SETTINGS_FIELD_LABELS[key], locale)}: "
                    f"{error.render(locale)}"
                    for key, error in entered.errors.items()
                },
                # Secrets after a save: whether one is stored now (the input is emptied).
                "secrets": {
                    field.key: bool(getattr(settings, field.key))
                    for field in section_fields(entered.section)
                    if field.kind == "secret"
                }
                if saved
                else {},
                "setup_html": setup.body.decode(),
            },
            status_code=200 if entered.ok else 400,
        )
    if saved and entered.ok:
        for message, category in results:
            flash(request, message, category)
        return RedirectResponse(f"/settings#{entered.section}", status_code=303)
    return render_settings(
        request,
        db,
        user,
        entered=entered,
        results=results,
        status_code=200 if entered.ok else 400,
    )


def _read(request: Request, db: Session, form: FormData, section: str):
    base = request.app.state.settings
    current = effective_settings(db, base)
    entered = read_section(form, section, current)
    draft = draft_settings(base, current, entered) if entered.ok else None
    return entered, draft


def _login_result(settings: Settings) -> tuple[Msg, str]:
    try:
        mail.check_login(settings)
    except mail.MailError as exc:
        return Msg("Mail login failed: {error}", error=str(exc)), "error"
    host = f"{settings.smtp_host}:{settings.smtp_port}"
    return Msg("Login to {host} works.", host=host), "success"


@router.post("/settings/section/{section}")
def save_section(
    section: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    entered, draft = _read(request, db, form, section)
    if draft is None:
        return _respond(request, db, user, entered, [(FIX_FIELDS, "error")])
    old_interval = effective_settings(db, request.app.state.settings).default_poll_interval_minutes
    store_section(db, entered, request.app.state.settings.secret_key.get_secret_value())
    settings = effective_settings(db, request.app.state.settings)
    if settings.default_poll_interval_minutes != old_interval:
        poll_plan.rescale(db, old_interval, settings, datetime.now(UTC))
    db.commit()
    results: Results = [(Msg("Settings saved."), "success")]
    if section == MAIL_SECTION and mail.has_login(settings):
        results.append(_login_result(settings))  # tell right away whether the login works
    return _respond(request, db, user, entered, results, saved=True)


@router.post("/settings/mail/check")
def check_mail_login(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    entered, draft = _read(request, db, form, MAIL_SECTION)
    results = [_login_result(draft)] if draft else [(FIX_FIELDS, "error")]
    return _respond(request, db, user, entered, results)


@router.post("/settings/mail/test")
def test_mail(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    entered, draft = _read(request, db, form, MAIL_SECTION)
    if draft is None:
        return _respond(request, db, user, entered, [(FIX_FIELDS, "error")])
    if not user.email:
        message = Msg("Add your e-mail address under Personal settings first.")
        return _respond(request, db, user, entered, [(message, "error")])
    test = mail_messages.test_mail(user.email, mail_messages.locale_of(user))
    try:
        mail.deliver(db, draft, replace(test, user_id=user.id), raise_errors=True)
    except mail.MailError as exc:
        results = [(Msg("Test mail not sent: {error}", error=str(exc)), "error")]
    else:
        results = [(Msg("Test mail sent to {email}.", email=user.email), "success")]
    db.commit()  # the attempt is in the mail log either way
    return _respond(request, db, user, entered, results)


@router.post("/settings/ai/test")
def test_ai(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    entered, draft = _read(request, db, form, AI_SECTION)
    if draft is None:
        return _respond(request, db, user, entered, [(FIX_FIELDS, "error")])
    results: Results
    try:
        answer = ai_prompts.connection_test(draft, ai.UsageLog(db, "test", user.id))
    except ai.AIError as exc:
        results = [(Msg("AI not reachable: {error}", error=str(exc)), "error")]
    else:
        seconds = f"{answer.seconds:.1f}"
        message = Msg(
            "AI answered in {seconds} s: {answer}", seconds=seconds, answer=answer.text[:80]
        )
        results = [(message, "success")]
        if draft.ai_provider == OLLAMA:
            results += ollama_report(draft)
    db.commit()  # the request is in the usage log either way
    return _respond(request, db, user, entered, results)
