"""Worker load on Platform Settings: measured poll durations against the poll interval."""

from datetime import UTC, datetime, timedelta

import pytest

from flighttracker.config import Settings
from flighttracker.models import FetchJob, JobKind, JobStatus
from flighttracker.services import capacity
from flighttracker.services.quota import search_requests
from flighttracker.services.searches import spec_of
from flighttracker.web.routes.settings import format_seconds
from tests.integration.test_services import A, create
from tests.integration.test_users import add_user, login_as
from tests.integration.test_web import client, login  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.fixture
def app_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused_test",
        secret_key="x" * 40,
        admin_username="admin",
        admin_password_hash="unused",
        flight_provider="google_flights",
        scraper_request_delay_seconds=3,
    )


def _job(db, search, *, status=JobStatus.DONE, minutes=None, run_after=NOW):
    started = NOW - timedelta(hours=3)
    db.add(
        FetchJob(
            search_id=search.id,
            kind=JobKind.POLL,
            status=status,
            run_after=run_after,
            started_at=started if minutes is not None else None,
            finished_at=started + timedelta(minutes=minutes) if minutes is not None else None,
        )
    )
    db.flush()


def test_measured_durations_and_extrapolation(db, app_settings):
    settings = app_settings.model_copy(update={"default_poll_interval_minutes": 60})
    polled = create(db)
    new = create(db, (A("GVA"),), (A("MAD"),))
    _job(db, polled, minutes=90)  # an older, longer poll does not count
    _job(db, polled, minutes=30)
    _job(db, new, status=JobStatus.QUEUED, run_after=NOW - timedelta(minutes=20))

    load = capacity.overview(db, settings, NOW)
    by_id = {item.id: item for item in load.items}
    requests = search_requests(spec_of(polled), settings)
    assert by_id[polled.id].measured_seconds == 30 * 60
    assert load.measured_seconds_per_request == 30 * 60 / requests
    # Never polled: extrapolated from the measured time per request.
    assert by_id[new.id].measured_seconds is None
    expected = search_requests(spec_of(new), settings) * load.seconds_per_request
    assert by_id[new.id].seconds == expected
    assert load.utilization == (30 * 60 + expected) / 3600
    assert (load.queued, load.longest_wait_seconds) == (1, 20 * 60)


def test_levels_and_reserve(db, app_settings):
    search = create(db)
    _job(db, search, minutes=50)
    for interval, level in ((60, "tight"), (40, "overloaded"), (24 * 60, "ok")):
        settings = app_settings.model_copy(update={"default_poll_interval_minutes": interval})
        load = capacity.overview(db, settings, NOW)
        assert load.level == level
    assert load.spare_seconds == (24 * 60 - 50) * 60
    assert load.spare_requests == int(load.spare_seconds // load.seconds_per_request)


def test_without_polls_the_configured_pause_is_used(db, app_settings):
    create(db)
    load = capacity.overview(db, app_settings, NOW)
    assert load.measured_seconds_per_request is None
    assert load.seconds_per_request == capacity.estimated_seconds(1, app_settings)


def test_format_seconds():
    assert [format_seconds(s) for s in (20, 12 * 60, 125 * 60)] == [
        "< 1 min",
        "12 min",
        "2 h 05 min",
    ]


def test_only_admins_see_the_worker_load(client, db):  # noqa: F811
    add_user(db, "alice")
    login_as(client, "alice")
    assert "Worker load" not in client.get("/settings").text
    client.post("/logout", data={"csrf_token": ""})
    client.cookies.clear()
    login(client)
    page = client.get("/settings").text
    assert "Worker load" in page and 'class="load-meter"' in page
