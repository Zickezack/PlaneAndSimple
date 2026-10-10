"""Price calendar end to end: the worker stores it, the detail page shows the latest prices."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from flighttracker.models import CalendarObservation, QueryKind, QueryLog
from flighttracker.providers.mock import MockProvider
from flighttracker.services import history
from flighttracker.web.flights import chart_data
from flighttracker.worker.__main__ import process_next_job, schedule
from tests.integration.test_services import create

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def _poll(db, session_factory, now):
    schedule(session_factory, clock=lambda: now)
    assert process_next_job(session_factory, MockProvider(clock=lambda: now), clock=lambda: now)
    db.expire_all()


def test_worker_stores_the_calendar_and_the_page_shows_the_latest_day_prices(db, session_factory):
    search = create(db, months_ahead=2, stay_days_min=7, stay_days_max=7, adults=2)
    db.commit()
    _poll(db, session_factory, NOW)

    calendar_log = db.scalars(select(QueryLog).where(QueryLog.kind == QueryKind.CALENDAR)).all()
    assert len(calendar_log) == 1  # one route, one cabin, one stay length
    stored = db.scalar(select(func.count()).select_from(CalendarObservation))
    # Every day from tomorrow (29 Sep) to the end of the last polled month (31 Oct).
    assert stored == calendar_log[0].quotes_stored == 33

    later = NOW + timedelta(days=1)
    search.next_poll_at = later
    db.commit()
    _poll(db, session_factory, later)

    days = history.calendar_days(db, search.id, NOW.date(), ("mock",), passengers=(2, 0))
    assert len(days) == 33  # one (the latest) price per day, past departures included
    first = min(days, key=lambda d: d.departure_date)
    assert (first.departure_date, first.stay_days, first.fake) == (date(2026, 9, 29), 7, True)
    assert history.calendar_days(db, search.id, NOW.date(), passengers=(1, 0)) == []

    data = chart_data([], date(2026, 9, 30), "CHF", {}, calendar=days)
    assert len(data["calendar"]) == 32  # upcoming departure days only
    assert data["calendar"][0] == {
        "o": "ZRH",
        "d": "BCN",
        "cabin": "economy",
        "date": "2026-09-30",
        "stay": 7,
        "price": data["calendar"][0]["price"],
        "fake": True,
    }
