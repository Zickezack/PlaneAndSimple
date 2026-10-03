"""Settings: personal settings (language, time zone, password) for every user; for admins
also Platform Settings – data export/import and DB-backed overrides of selected `.env`
defaults (flight data providers, worker, airport import, user limits)."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from flighttracker.i18n import SUPPORTED_LOCALES, Msg
from flighttracker.models import User
from flighttracker.providers.registry import PROVIDERS
from flighttracker.services import quota
from flighttracker.services.airport_import import import_from_ourairports
from flighttracker.services.data_transfer import export_all, import_payload
from flighttracker.services.settings import (
    AIRPORT_IMPORT_SECTION,
    PROVIDERS_SECTION,
    SEARCHES_SECTION,
    SETTING_FIELDS,
    USERS_SECTION,
    WORKER_SECTION,
    SettingField,
    SettingValidationError,
    clear_override,
    effective_settings,
    load_overrides,
    set_override,
)
from flighttracker.services.users import (
    UserValidationError,
    change_own_password,
    set_preferences,
)
from flighttracker.web import labels
from flighttracker.web.deps import (
    SESSION_AUTH_VERSION,
    SESSION_LOCALE,
    AuthUser,
    csrf_form,
    flash,
    get_db,
    get_user,
    require_admin,
    require_login,
)
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])
admin_only = [Depends(require_admin)]

_SECTIONS = (
    PROVIDERS_SECTION,
    SEARCHES_SECTION,
    WORKER_SECTION,
    AIRPORT_IMPORT_SECTION,
    USERS_SECTION,
)


def _section_fields(section: str) -> list[SettingField]:
    return [field for field in SETTING_FIELDS if field.section == section]


def _field_view(field: SettingField, overrides: dict, settings) -> dict:
    value = getattr(settings, field.key)
    is_secret = field.kind == "secret"
    return {
        "key": field.key,
        "label": labels.SETTINGS_FIELD_LABELS[field.key],
        "hint": labels.SETTINGS_FIELD_HINTS[field.key],
        "is_secret": is_secret,
        "is_choice": field.key == "flight_provider",
        "is_overridden": field.key in overrides,
        "value": "" if is_secret else value,
        "has_value": bool(value.get_secret_value()) if is_secret and value else False,
    }


def _redirect() -> RedirectResponse:
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings")
def index(request: Request, db: Session = Depends(get_db), user: User = Depends(get_user)):
    settings = effective_settings(db, request.app.state.settings)
    sections = []
    if user.is_admin:
        overrides = load_overrides(db)
        sections = [
            {
                "key": section,
                "label": labels.SETTINGS_SECTION_LABELS[section],
                "fields": [
                    _field_view(field, overrides, settings) for field in _section_fields(section)
                ],
            }
            for section in _SECTIONS
        ]
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        {
            "sections": sections,
            "provider_options": sorted(PROVIDERS),
            "user": user,
            "usage": quota.usage(db, user, settings),
            "locale_options": list(SUPPORTED_LOCALES.items()),
            "default_timezone": settings.display_timezone,
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
    except UserValidationError as exc:
        for error in exc.errors:
            flash(request, error, "error")
        return _redirect()
    db.commit()
    if locale:
        request.session[SESSION_LOCALE] = locale
    flash(request, Msg("Settings saved."), "success")
    return _redirect()


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
        return _redirect()
    try:
        change_own_password(user, str(form.get("current_password", "")), new)
    except UserValidationError as exc:
        for error in exc.errors:
            flash(request, error, "error")
        return _redirect()
    db.commit()
    # Other sessions of this user end; this one continues.
    request.session[SESSION_AUTH_VERSION] = user.auth_version
    flash(request, Msg("Password changed. Other sessions were logged out."), "success")
    return _redirect()


@router.post("/settings/section/{section}", dependencies=admin_only)
def save_section(
    section: str,
    request: Request,
    form: FormData = Depends(csrf_form),
    db: Session = Depends(get_db),
):
    if section not in _SECTIONS:
        raise HTTPException(status_code=404)
    errors: list[str] = []
    for field in _section_fields(section):
        if field.kind == "secret" and form.get(f"{field.key}_clear") == "1":
            clear_override(db, field.key)
            continue
        raw = str(form.get(field.key, "")).strip()
        if field.kind == "secret" and not raw:
            continue  # blank means "keep the current value", not "clear it"
        try:
            set_override(db, field.key, raw)
        except SettingValidationError as exc:
            errors.append(f"{labels.SETTINGS_FIELD_LABELS[field.key]}: {exc}")
    if errors:
        db.rollback()
        flash(request, Msg("Could not save: {errors}", errors="; ".join(errors)), "error")
    else:
        db.commit()
        flash(request, Msg("Settings saved."), "success")
    return _redirect()


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
        return _redirect()
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
    return _redirect()


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
        return _redirect()
    try:
        payload = json.loads(upload.file.read())
    except (ValueError, UnicodeDecodeError):
        flash(request, Msg("This file is not valid JSON."), "error")
        return _redirect()
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
    return _redirect()
