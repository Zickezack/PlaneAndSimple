"""Poll schedule (admins only): when within the poll interval each active Suchabo and Trip is
polled. A timeline (bars can be dragged, static/js/poll-schedule.js) above a form with one start
time per poll – the form alone works without JavaScript."""

import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.domain.poll_plan import DAY_MINUTES, offset_parts, parse_offset
from flighttracker.i18n import Msg
from flighttracker.services import poll_plan
from flighttracker.services.settings import effective_settings
from flighttracker.web.deps import csrf_form, flash, get_db, require_admin
from flighttracker.web.routes.settings import format_seconds
from flighttracker.web.templating import templates

router = APIRouter(dependencies=[Depends(require_admin)])

# Timeline geometry in SVG units (the SVG scales to the page width).
WIDTH = 1000
AXIS = 22
LANE = 26
BAR = 20
MIN_BAR = 4
# Tick spacing: the first that gives at most 8 ticks per interval.
_TICK_STEPS = (15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080)


def _ticks(interval: int) -> list[dict]:
    step = next((s for s in _TICK_STEPS if interval / s <= 8), interval)
    return [
        {"x": minute / interval * WIDTH, "day": day, "clock": clock}
        for minute in range(0, interval, step)
        for day, clock in [offset_parts(minute, interval)]
    ]


def _bars(plan: poll_plan.Schedule) -> list[dict]:
    """One bar per placed poll; a poll running past the end of the interval wraps around."""
    interval = plan.cycle.interval_minutes
    bars = []
    for number, poll in enumerate(plan.polls, start=1):
        if poll.offset is None:
            continue
        x = poll.offset / interval * WIDTH
        width = max(poll.minutes / interval * WIDTH, MIN_BAR)
        segments = [(x, min(width, WIDTH - x))]
        if x + width > WIDTH:
            segments.append((0, x + width - WIDTH))
        bars.append(
            {"number": number, "poll": poll, "y": AXIS + poll.lane * LANE, "segments": segments}
        )
    return bars


def _redirect() -> RedirectResponse:
    return RedirectResponse("/settings/schedule", status_code=303)


@router.get("/settings/schedule")
def show(request: Request, db: Session = Depends(get_db)):
    settings = effective_settings(db, request.app.state.settings)
    plan = poll_plan.schedule(db, settings, datetime.now(UTC))
    interval = plan.cycle.interval_minutes
    return templates.TemplateResponse(
        request,
        "settings/schedule.html",
        {
            "plan": plan,
            "interval": interval,
            "days": math.ceil(interval / DAY_MINUTES) if interval > DAY_MINUTES else 0,
            "offset_parts": lambda offset: offset_parts(offset, interval),
            "ticks": _ticks(interval),
            "bars": _bars(plan),
            "height": AXIS + plan.lanes * LANE + 4,
            "now_x": plan.now_minutes / interval * WIDTH,
            "geometry": {"width": WIDTH, "axis": AXIS, "lane": LANE, "bar": BAR},
            # For the drag and drop script: the same data the bars are drawn from.
            "model": [
                {
                    "key": poll.key,
                    "number": number,
                    "name": poll.load.name,
                    "start": poll.offset,
                    "minutes": poll.minutes,
                }
                for number, poll in enumerate(plan.polls, start=1)
            ],
            "duration": format_seconds,
            "zone": settings.display_timezone,
        },
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
