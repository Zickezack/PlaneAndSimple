"""Poll schedule: automatic placement, admin changes, and the scheduler following the plan."""

from datetime import UTC, date, datetime, timedelta

import pytest

from flighttracker.config import Settings
from flighttracker.domain.filters import CabinClass
from flighttracker.services import jobs, poll_plan
from flighttracker.services.trips import TripInput, TripLegInput, create_trip
from flighttracker.worker.loop import schedule
from tests.integration.test_services import A, create
from tests.integration.test_users import add_user, login_as
from tests.integration.test_web import client, csrf, login  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 13, 10, tzinfo=UTC)  # 12:00 in Zurich


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused_test",
        secret_key="x" * 40,
        admin_username="admin",
        admin_password_hash="unused",
        default_poll_interval_minutes=1440,
    )


def zurich(*args) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(*args, tzinfo=ZoneInfo("Europe/Zurich")).astimezone(UTC)


def _trip(db):
    starts_on = date(2026, 11, 1)
    return create_trip(
        db,
        TripInput(
            name="Loop",
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=7),
            poll_interval_minutes=1440,
            legs=(TripLegInput("ZRH", "BCN"), TripLegInput("BCN", "ZRH")),
            cabin_classes=frozenset({CabinClass.ECONOMY}),
            max_stops=None,
            adults=1,
            currency="CHF",
        ),
        max_route_pairs=50,
    )


def test_new_searches_and_trips_are_placed_apart(db, settings):
    first = create(db)
    second = create(db, (A("GVA"),), (A("MAD"),))
    trip = _trip(db)
    assert poll_plan.place_unplanned(db, settings, NOW) == 3
    assert poll_plan.place_unplanned(db, settings, NOW) == 0
    offsets = sorted(
        [first.poll_offset_minutes, second.poll_offset_minutes, trip.poll_offset_minutes]
    )
    assert offsets[0] == 0
    assert len(set(offsets)) == 3
    # Trip legs are polled through their Trip and never get an own start.
    assert all(leg.search.poll_offset_minutes is None for leg in trip.legs)


def test_scheduler_follows_the_planned_start(db, settings):
    search = create(db)  # due right away
    search.poll_offset_minutes = 7 * 60
    cycle = poll_plan.poll_cycle(settings)
    assert jobs.schedule_due_polls(db, NOW, cycle=cycle) == 1
    # The first poll ran at 12:00; 07:00 tomorrow is the next planned start.
    assert search.next_poll_at == zurich(2026, 10, 14, 7, 0)
    assert jobs.schedule_due_polls(db, zurich(2026, 10, 14, 7, 0), cycle=cycle) == 0  # still open
    assert not jobs.request_poll(db, search, NOW, cycle=cycle)


def test_moving_a_start_replans_the_waiting_poll(db, settings):
    search = create(db)
    search.poll_offset_minutes = 0
    search.last_polled_at = zurich(2026, 10, 13, 9, 0)
    search.next_poll_at = zurich(2026, 10, 14, 0, 0)
    assert poll_plan.set_offsets(db, settings, NOW, {f"search-{search.id}": 13 * 60}) == 1
    # 13:00 today is less than half an interval after the 09:00 poll – tomorrow 13:00.
    assert search.next_poll_at == zurich(2026, 10, 14, 13, 0)
    assert poll_plan.set_offsets(db, settings, NOW, {f"search-{search.id}": 22 * 60}) == 1
    assert search.next_poll_at == zurich(2026, 10, 13, 22, 0)
    # Unknown keys and starts outside the interval are ignored.
    assert poll_plan.set_offsets(db, settings, NOW, {"search-0": 5, "trip-x": 5}) == 0
    assert poll_plan.set_offsets(db, settings, NOW, {f"search-{search.id}": 1440}) == 0


def test_spread_and_rescale(db, settings):
    searches = [create(db), create(db, (A("GVA"),), (A("MAD"),))]
    for search, offset in zip(searches, (60, 70), strict=True):
        search.poll_offset_minutes = offset
    poll_plan.spread_all(db, settings, NOW)
    first, second = (s.poll_offset_minutes for s in searches)
    assert first == 0 and second > 600  # same order, far apart now
    poll_plan.rescale(
        db, 1440, settings.model_copy(update={"default_poll_interval_minutes": 360}), NOW
    )
    assert searches[1].poll_offset_minutes == round(second / 4)


def test_worker_places_new_searches(db, session_factory, settings):
    search = create(db)
    db.commit()
    schedule(session_factory, clock=lambda: NOW, base_settings=settings)
    db.refresh(search)
    assert search.poll_offset_minutes == 0
    assert search.next_poll_at == zurich(2026, 10, 14, 0, 0)


def test_schedule_page_for_admins_only(client, db):  # noqa: F811
    search = create(db)
    search.poll_offset_minutes = 120
    db.commit()
    add_user(db, "alice")
    login_as(client, "alice")
    assert client.get("/settings/schedule").status_code == 403

    login(client)
    page = client.get("/settings/schedule").text
    assert f'name="start-search-{search.id}" value="02:00"' in page
    assert "schedule-bar" in page
    response = client.post(
        "/settings/schedule",
        data={"csrf_token": csrf(page), f"start-search-{search.id}": "05:30"},
    )
    assert response.status_code == 200
    db.refresh(search)
    assert search.poll_offset_minutes == 5 * 60 + 30

    page = client.get("/settings/schedule").text
    client.post(
        "/settings/schedule",
        data={"csrf_token": csrf(page), f"start-search-{search.id}": "25:00"},
    )
    db.refresh(search)
    assert search.poll_offset_minutes == 5 * 60 + 30
    assert (
        client.post("/settings/schedule/spread", data={"csrf_token": csrf(page)}).status_code == 200
    )


def test_starts_beyond_a_shorter_interval_wrap_around(db, settings):
    search = create(db)
    search.poll_offset_minutes = 1380  # stored for 24 h, the interval is now 6 h
    short = settings.model_copy(update={"default_poll_interval_minutes": 360})
    (poll,) = poll_plan.schedule(db, short, NOW).polls
    assert poll.offset == 1380 % 360


def test_resumed_search_moves_only_when_its_start_is_taken(db, settings):
    from flighttracker.services.searches import pause_search, resume_search

    kept, moved, other = (
        create(db),
        create(db, (A("GVA"),), (A("MAD"),)),
        create(db, (A("ZRH"),), (A("MAD"),)),
    )
    kept.poll_offset_minutes, moved.poll_offset_minutes, other.poll_offset_minutes = 300, 0, 0
    for search in (kept, moved):
        pause_search(search)
    db.flush()
    resume_search(kept, NOW)
    assert not poll_plan.fit_returning(db, settings, NOW, f"search-{kept.id}")
    assert kept.poll_offset_minutes == 300
    resume_search(moved, NOW)
    assert poll_plan.fit_returning(db, settings, NOW, f"search-{moved.id}")
    assert moved.poll_offset_minutes not in (0, 300)


def test_resume_button_fits_the_search_in(client, db):  # noqa: F811
    from flighttracker.services.searches import pause_search

    paused, other = create(db), create(db, (A("GVA"),), (A("MAD"),))
    paused.poll_offset_minutes = other.poll_offset_minutes = 60
    pause_search(paused)
    db.commit()
    token = login(client)
    client.post(f"/searches/{paused.id}/resume", data={"csrf_token": token})
    db.refresh(paused)
    assert paused.poll_offset_minutes != 60


def test_settings_page_shows_the_schedule_read_only(client, db):  # noqa: F811
    search = create(db)
    search.poll_offset_minutes = 90
    db.commit()
    login(client)
    page = client.get("/settings").text
    assert "Worker load and poll schedule" in page
    assert 'class="schedule-timeline"' in page  # without "is-editable"
    assert 'href="/settings/schedule"' in page and "Edit schedule" in page
    assert "<td>01:30</td>" in page
    assert 'name="start-search-' not in page
