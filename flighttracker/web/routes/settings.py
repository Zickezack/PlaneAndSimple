"""Settings: personal settings (language, time zone, password) for every user; for admins
also Platform Settings – data export/import and DB-backed overrides of selected `.env`
defaults (flight data providers, worker, airport import, user limits)."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from flighttracker.domain.mail_providers import MAIL_PROVIDERS
from flighttracker.i18n import SUPPORTED_LOCALES, Msg
from flighttracker.models import User
from flighttracker.services import capacity, poll_plan, quota
from flighttracker.services.airport_import import import_from_ourairports
from flighttracker.services.data_export import export_all
from flighttracker.services.data_import import import_payload
from flighttracker.services.settings import (
    SETTING_FIELDS,
    SettingValidationError,
    clear_override,
    effective_settings,
    load_overrides,
    set_override,
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
    mail_preset,
    section_fields,
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
    settings = effective_settings(db, request.app.state.settings)
    locale = current_locale(request)
    sections = []
    load = schedule_context = None
    if user.is_admin:
        now = datetime.now(UTC)
        load = capacity.overview(db, settings, now)
        schedule_context = timeline(poll_plan.schedule(db, settings, now, load))
        overrides = load_overrides(db)
        sections = [
            {
                "key": section,
                "label": labels.SETTINGS_SECTION_LABELS[section],
                "fields": [
                    field_view(field, overrides, settings) for field in section_fields(section)
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
            "zone": settings.display_timezone,
            "duration": format_seconds,
            "locale_options": list(SUPPORTED_LOCALES.items()),
            "currency_options": currency_options(locale, settings.default_currency),
            "mail_providers": MAIL_PROVIDERS,
            "timezone_options": timezone_options(settings.display_timezone, locale),
        },
    )


@router.post("/settings/personal")
def save_personal(
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
    user: User = Depends(get_user),
):
    locale = str(form.get("locale", ""))
    try:
        set_preferences(
            user,
            locale=locale or None,
            timezone=str(form.get("timezone", "")).strip(),
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
    send_password_changed(request, effective_settings(db, request.app.state.settings), user)
    # Other sessions of this user end; this one continues.
    request.session[SESSION_AUTH_VERSION] = user.auth_version
    flash(request, Msg("Password changed. Other sessions were logged out."), "success")
    return _redirect("password")


@router.post("/settings/section/{section}", dependencies=admin_only)
def save_section(
    section: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    old_interval = effective_settings(db, request.app.state.settings).default_poll_interval_minutes
    errors: list[str] = []
    values = {field.key: str(form.get(field.key, "")).strip() for field in SETTING_FIELDS}
    values |= mail_preset(str(form.get("smtp_preset", "")))
    secret_key = request.app.state.settings.secret_key.get_secret_value()
    for field in section_fields(section):
        if field.kind == "secret" and form.get(f"{field.key}_clear") == "1":
            clear_override(db, field.key)
            continue
        raw = values[field.key]
        if field.kind == "secret" and not raw:
            continue  # blank means "keep the current value", not "clear it"
        try:
            set_override(db, field.key, raw, secret_key=secret_key)
        except SettingValidationError as exc:
            errors.append(f"{labels.SETTINGS_FIELD_LABELS[field.key]}: {exc}")
    if errors:
        db.rollback()
        flash(request, Msg("Could not save: {errors}", errors="; ".join(errors)), "error")
        return _redirect(section)
    settings = effective_settings(db, request.app.state.settings)
    if settings.default_poll_interval_minutes != old_interval:
        poll_plan.rescale(db, old_interval, settings, datetime.now(UTC))
    db.commit()
    flash(request, Msg("Settings saved."), "success")
    return _redirect(section)


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
