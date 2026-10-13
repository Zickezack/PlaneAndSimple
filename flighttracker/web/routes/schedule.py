"""Editing the poll schedule (admins only; the read-only view is part of Worker load on the
Settings page). A timeline (bars can be dragged, static/js/poll-schedule.js) above a form with
one start time per poll – the form alone works without JavaScript."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.domain.poll_plan import parse_offset
from flighttracker.i18n import Msg
from flighttracker.services import poll_plan
from flighttracker.services.settings import effective_settings
from flighttracker.web.deps import csrf_form, flash, get_db, require_admin
from flighttracker.web.routes.settings import format_seconds
from flighttracker.web.schedule_view import timeline
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _redirect() -> RedirectResponse:
    return RedirectResponse("/settings/schedule", status_code=303)


@router.get("/settings/schedule")
def show(request: Request, db: Session = Depends(get_db)):
    settings = effective_settings(db, request.app.state.settings)
    plan = poll_plan.schedule(db, settings, datetime.now(UTC))
    return templates.TemplateResponse(
        request,
        "settings/schedule.html",
        {**timeline(plan), "duration": format_seconds, "zone": settings.display_timezone},
    )


def parse_schedule_form(form: FormData, interval: int) -> tuple[dict[str, int], list[Msg]]:
    """Start per poll key from the fields `start-<key>` (HH:MM) and `day-<key>` (1-based)."""
    offsets: dict[str, int] = {}
    errors: list[Msg] = []
    for field, value in form.multi_items():
        if not field.startswith("start-") or not str(value).strip():
            continue
        key = field.removeprefix("start-")
        try:
            day = int(str(form.get(f"day-{key}") or 1))
            offsets[key] = parse_offset(day, str(value), interval)
        except ValueError:
            errors.append(
                Msg('Invalid start "{value}" – use HH:MM within the poll interval.', value=value)
            )
    return offsets, errors


@router.post("/settings/schedule")
def save(request: Request, form: FormData = Depends(csrf_form), db: Session = Depends(get_db)):
    settings = effective_settings(db, request.app.state.settings)
    offsets, errors = parse_schedule_form(form, settings.default_poll_interval_minutes)
    if errors:
        for error in errors:
            flash(request, error, "error")
        return _redirect()
    changed = poll_plan.set_offsets(db, settings, datetime.now(UTC), offsets)
    db.commit()
    flash(request, Msg("Poll schedule saved ({n} changed).", n=changed), "success")
    return _redirect()


@router.post("/settings/schedule/spread")
def spread(request: Request, form: FormData = Depends(csrf_form), db: Session = Depends(get_db)):
    settings = effective_settings(db, request.app.state.settings)
    poll_plan.spread_all(db, settings, datetime.now(UTC))
    db.commit()
    flash(request, Msg("Polls spread evenly over the poll interval."), "success")
    return _redirect()
