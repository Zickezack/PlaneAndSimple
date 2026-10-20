"""Settings: personal settings (language, time zone, password) for every user; for admins
also Platform Settings – data export/import and DB-backed overrides of selected `.env`
defaults (flight data providers, worker, airport import, user limits)."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from flighttracker.domain.ai_providers import AI_PRESETS
from flighttracker.domain.mail_providers import MAIL_PROVIDERS
from flighttracker.i18n import SUPPORTED_LOCALES, Msg
from flighttracker.models import User
from flighttracker.services import capacity, poll_plan, quota, usage_logs
from flighttracker.services.ai_prompts import DEFAULT_SYSTEM_PROMPT
from flighttracker.services.airport_import import import_from_ourairports
from flighttracker.services.data_export import export_all
from flighttracker.services.data_import import import_payload
from flighttracker.services.settings import (
    effective_settings,
    load_overrides,
)
from flighttracker.services.users import (
    UserValidationError,
    change_own_password,
    set_email,
    set_preferences,
)
from flighttracker.web import labels
from flighttracker.web.deps import (
    SESSION_AUTH_VERSION,
    SESSION_LOCALE,
    AuthUser,
    csrf_form,
    current_locale,
    flash,
    get_db,
    get_user,
    require_admin,
    require_login,
)
from flighttracker.web.mails import send_password_changed
from flighttracker.web.schedule_view import timeline
from flighttracker.web.settings_forms import (
    SECTIONS,
    currency_options,
    field_view,
    section_fields,
    setup_status,
    timezone_options,
)
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])
admin_only = [Depends(require_admin)]


def format_seconds(seconds: float) -> str:
    """Poll durations: "2 h 05 min", "12 min", "< 1 min"."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "< 1 min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _redirect(section: str) -> RedirectResponse:
    """Back to the Settings page, scrolled to (and with JavaScript unfolding) the section."""
    return RedirectResponse(f"/settings#{section}", status_code=303)


@router.get("/settings")
def index(request: Request, db: Session = Depends(get_db), user: User = Depends(get_user)):
    return render_settings(request, db, user)


def render_settings(
    request: Request,
    db: Session,
    user: User,
    *,
    entered=None,
    results: list[tuple[Msg, str]] | None = None,
    status_code: int = 200,
):
    """The Settings page. `entered` (web/settings_sections.SectionInput): show that section
    open with the entered values, field errors and `results` – used without JavaScript, so a
    test or a failed save never loses what was typed."""
    settings = effective_settings(db, request.app.state.settings)
    locale = current_locale(request)
    sections = []
    load = schedule_context = None
    logs = {}
    if user.is_admin:
        now = datetime.now(UTC)
        load = capacity.overview(db, settings, now)
        schedule_context = timeline(poll_plan.schedule(db, settings, now, load))
        logs = {
            "mail_log": usage_logs.mail_overview(db, now),
            "ai_usage": usage_logs.ai_overview(db, now),
        }
        overrides = load_overrides(db)
        sections = [
            {
                "key": section,
                "label": labels.SETTINGS_SECTION_LABELS[section],
                "fields": [
                    field_view(
                        field,
                        overrides,
                        settings,
                        entered if entered and entered.section == section else None,
                    )
                    for field in section_fields(section)
                ],
            }
            for section in SECTIONS
        ]
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "sections": sections,
            "user": user,
            "usage": quota.usage(db, user, settings),
            "capacity": load,
            # Quick navigation: only admins have enough sections for it.
            "section_nav": [
                ("personal", "Personal settings"),
                ("password", "Change password"),
                ("schedule", "Worker load and poll schedule"),
                ("transfer", "Export and import"),
            ]
            + [(section["key"], section["label"]) for section in sections]
            if user.is_admin
            else None,
            # "Edit schedule" without JavaScript: the same page with the start time fields.
            "editing": user.is_admin and request.query_params.get("edit") == "schedule",
            # Read-only poll schedule inside the Worker load card.
            **(schedule_context or {}),
            **logs,
            "open_section": entered.section if entered else None,
            "section_results": {entered.section: results or []} if entered else {},
            "zone": settings.display_timezone,
            "duration": format_seconds,
            "locale_options": list(SUPPORTED_LOCALES.items()),
            "currency_options": currency_options(locale, settings.default_currency),
            "mail_providers": MAIL_PROVIDERS,
            "setup": setup_status(settings) if user.is_admin else [],
            "ai_presets": AI_PRESETS,
            "default_ai_prompt": DEFAULT_SYSTEM_PROMPT,
            "timezone_options": timezone_options(settings.display_timezone, locale),
        },
        status_code=status_code,
    )


@router.post("/settings/personal")
def save_personal(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    locale = str(form.get("locale", ""))
    deal_mails = form.get("deal_mails")
    try:
        set_preferences(
            user,
            locale=locale or None,
            timezone=str(form.get("timezone", "")).strip(),
            deal_mails=None if deal_mails is None else deal_mails == "true",
        )
        set_email(db, user, str(form.get("email", "")))
    except UserValidationError as exc:
        for error in exc.errors:
            flash(request, error, "error")
        return _redirect("personal")
    db.commit()
    if locale:
        request.session[SESSION_LOCALE] = locale
    flash(request, Msg("Settings saved."), "success")
    return _redirect("personal")


@router.post("/settings/password")
def change_password(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    new = str(form.get("new_password", ""))
    if new != str(form.get("repeat_password", "")):
        flash(request, Msg("The new passwords do not match."), "error")
        return _redirect("password")
    try:
        change_own_password(user, str(form.get("current_password", "")), new)
    except UserValidationError as exc:
        for error in exc.errors:
            flash(request, error, "error")
        return _redirect("password")
    db.commit()
    send_password_changed(request, db, effective_settings(db, request.app.state.settings), user)
    # Other sessions of this user end; this one continues.
    request.session[SESSION_AUTH_VERSION] = user.auth_version
    flash(request, Msg("Password changed. Other sessions were logged out."), "success")
    return _redirect("password")


@router.post("/settings/airport-import/run", dependencies=admin_only)
def run_airport_import(
    request: Request, form: FormData = Depends(csrf_form), db: Session = Depends(get_db)
):
    settings = effective_settings(db, request.app.state.settings)
    try:
        countries, airports, ranked = import_from_ourairports(
            db, wikidata_contact=settings.wikidata_contact
        )
    except Exception as exc:
        db.rollback()
        flash(request, Msg("Airport import failed ({error}).", error=type(exc).__name__), "error")
        return _redirect("airport_import")
    db.commit()
    flash(
        request,
        Msg(
            "Imported {countries} countries and {airports} airports "
            "({ranked} with passenger numbers).",
            countries=countries,
            airports=airports,
            ranked=ranked,
        ),
        "success",
    )
    return _redirect("airport_import")


@router.get("/settings/export", dependencies=admin_only)
def export_data(db: Session = Depends(get_db)) -> Response:
    payload = export_all(db)
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="plane-and-simple-export.json"'},
    )


@router.post("/settings/import", dependencies=admin_only)
def import_data(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: AuthUser = Depends(require_login),
):
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        flash(request, Msg("Please choose a JSON file to import."), "error")
        return _redirect("transfer")
    try:
        payload = json.loads(upload.file.read())
    except (ValueError, UnicodeDecodeError):
        flash(request, Msg("This file is not valid JSON."), "error")
        return _redirect("transfer")
    result = import_payload(db, payload, owner_id=user.id)
    if result.errors:
        db.rollback()
        flash(request, Msg("Import failed: {errors}", errors="; ".join(result.errors)), "error")
    else:
        db.commit()
        flash(
            request,
            Msg(
                "Import done: {created} new tracked searches, {prices} new prices, "
                "{logs} new log entries, {trips} new Trips ({matched} tracked searches "
                "and {trips_matched} Trips already existed).",
                created=result.searches_created,
                prices=result.prices_added,
                logs=result.logs_added,
                trips=result.trips_created,
                matched=result.searches_matched,
                trips_matched=result.trips_matched,
            ),
            "success",
        )
    return _redirect("transfer")
