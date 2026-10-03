from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from flighttracker.config import Settings
from flighttracker.domain.filters import CabinClass, TripType
from flighttracker.models import FetchJob, PriceHistory, QueryLog
from flighttracker.providers.mock import MockProvider
from flighttracker.services.jobs import (
    request_trip_poll,
    schedule_due_polls,
    schedule_due_trip_polls,
)
from flighttracker.services.settings import set_override
from flighttracker.services.trips import (
    TripInput,
    TripLegInput,
    TripValidationError,
    create_trip,
    itineraries_for_trip,
    validate_trip,
)
from flighttracker.worker.__main__ import process_next_job


def trip_input():
    starts_on = date.today() + timedelta(days=14)
    return TripInput(
        name="Island loop",
        starts_on=starts_on,
        ends_on=starts_on + timedelta(days=7),
        poll_interval_minutes=360,
        legs=(
            TripLegInput("ZRH", "BCN"),
            TripLegInput("BCN", "MAD", min_layover_days=1, max_layover_days=1),
            TripLegInput("MAD", "ZRH", min_layover_days=1),
        ),
        cabin_classes=frozenset({CabinClass.ECONOMY}),
        max_stops=1,
        adults=1,
        currency="CHF",
    )


def test_trip_creates_ordered_independent_one_way_searches_and_shared_poll(db):
    trip = create_trip(db, trip_input(), max_route_pairs=50)
    db.flush()

    assert [leg.position for leg in trip.legs] == [0, 1, 2]
    assert [leg.search.filters["trip_type"] for leg in trip.legs] == [TripType.ONE_WAY] * 3
    assert [leg.search.next_poll_at for leg in trip.legs] == [None, None, None]
    assert trip.legs[1].min_layover_days == 1
    assert trip.legs[2].min_layover_days == 1

    now = datetime.now(UTC) + timedelta(minutes=1)
    assert schedule_due_polls(db, now) == 0
    assert schedule_due_trip_polls(db, now) == 1
    jobs = db.scalars(select(FetchJob).where(FetchJob.trip_id == trip.id)).all()
    assert len(jobs) == 1
    assert jobs[0].search_id == trip.legs[0].search_id


def test_trip_date_window_cannot_exceed_configured_bound(db):
    from dataclasses import replace

    from flighttracker.services.trips import TripValidationError, validate_trip

    data = replace(trip_input(), ends_on=trip_input().starts_on + timedelta(days=60))
    try:
        validate_trip(data, date.today())
    except TripValidationError as exc:
        assert "cannot exceed" in str(exc)
    else:
        raise AssertionError("expected trip window validation to fail")


def test_trip_enforces_nine_passenger_limit():
    from dataclasses import replace

    data = replace(trip_input(), adults=8, children=2)
    with pytest.raises(TripValidationError, match="At most 9 passengers"):
        validate_trip(data, date.today())


def test_longest_trip_name_still_fits_every_leg_name(db):
    from dataclasses import replace

    longest = replace(trip_input(), name="T" * 100)
    validate_trip(longest, date.today())
    create_trip(db, longest, max_route_pairs=50)
    db.flush()  # leg names "<trip> · 3 MAD–ZRH" must fit searches.name
    with pytest.raises(TripValidationError, match="at most 100 characters"):
        validate_trip(replace(trip_input(), name="T" * 101), date.today())


def test_expired_trip_is_not_scheduled(db):
    trip = create_trip(db, trip_input(), max_route_pairs=50)
    today = date.today()
    trip.starts_on = today - timedelta(days=8)
    trip.ends_on = today - timedelta(days=1)
    trip.next_poll_at = datetime.now(UTC) - timedelta(days=1)
    db.flush()

    assert schedule_due_trip_polls(db, datetime.now(UTC)) == 0


def test_coordinated_trip_poll_stores_only_feasible_legs(db, session_factory):
    trip = create_trip(db, trip_input(), max_route_pairs=50)
    db.commit()
    now = datetime.now(UTC)
    assert request_trip_poll(db, trip, now)
    db.commit()

    provider = MockProvider(clock=lambda: now)
    assert process_next_job(session_factory, provider, clock=lambda: now)

    db.refresh(trip)
    assert itineraries_for_trip(db, trip)


def test_trip_worker_queries_each_selected_country_route(db, session_factory):
    starts_on = date.today() + timedelta(days=14)
    trip = create_trip(
        db,
        TripInput(
            name="Country connections",
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=7),
            poll_interval_minutes=1440,
            legs=(
                TripLegInput(
                    "CH",
                    "ES",
                    country_airports={
                        "CH": frozenset({"ZRH", "GVA"}),
                        "ES": frozenset({"BCN", "MAD"}),
                    },
                ),
                TripLegInput(
                    "ES",
                    "CH",
                    min_layover_days=1,
                    country_airports={
                        "CH": frozenset({"ZRH", "GVA"}),
                        "ES": frozenset({"BCN", "MAD"}),
                    },
                ),
            ),
            cabin_classes=frozenset({CabinClass.ECONOMY}),
            max_stops=None,
            adults=1,
            currency="CHF",
        ),
        max_route_pairs=50,
    )
    first_search_id = trip.legs[0].search_id
    db.commit()
    assert request_trip_poll(db, trip, datetime.now(UTC))
    db.commit()

    assert process_next_job(session_factory, MockProvider())
    routes = set(
        db.execute(
            select(QueryLog.origin_iata, QueryLog.destination_iata).where(
                QueryLog.search_id == first_search_id
            )
        )
    )
    assert routes == {
        ("ZRH", "BCN"),
        ("ZRH", "MAD"),
        ("GVA", "BCN"),
        ("GVA", "MAD"),
    }


def test_trip_worker_uses_global_currency(db, session_factory):
    trip = create_trip(db, trip_input(), max_route_pairs=50)
    first_search_id = trip.legs[0].search_id
    set_override(db, "default_currency", "EUR")
    db.commit()
    now = datetime.now(UTC)
    assert request_trip_poll(db, trip, now)
    db.commit()

    base_settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused/unused_test",
        secret_key="x" * 40,
        admin_username="admin",
        admin_password_hash="unused",
    )
    assert process_next_job(
        session_factory,
        MockProvider(clock=lambda: now),
        clock=lambda: now,
        base_settings=base_settings,
    )
    currencies = set(
        db.scalars(select(PriceHistory.currency).where(PriceHistory.search_id == first_search_id))
    )
    assert currencies == {"EUR"}
