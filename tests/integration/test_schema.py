from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.domain.locations import LocationRef
from flighttracker.domain.spec import SearchSpec
from flighttracker.models import Base, PriceHistory, PriceSource, SearchLocation, SearchRevision
from flighttracker.services.searches import SearchInput, create_search, current_revision_id
from tests.integration.conftest import _alembic_config

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def _search(db):
    spec = SearchSpec(
        frozenset({LocationRef.airport("ZRH")}),
        frozenset({LocationRef.airport("BCN")}),
        SearchFilters(),
    )
    return create_search(db, SearchInput("Test", spec, 360), max_route_pairs=50, now=NOW)


def _price_row(search_id, revision_id, **overrides):
    values = {
        "search_id": search_id,
        "search_revision_id": revision_id,
        "source": PriceSource.LIVE,
        "provider": "mock",
        "origin_iata": "ZRH",
        "destination_iata": "BCN",
        "departure_date": date(2026, 11, 3),
        "return_date": None,
        "cabin_class": CabinClass.ECONOMY,
        "stops": 0,
        "price": Decimal("99.00"),
        "currency": "CHF",
        "adults": 1,
        "children": 0,
        "observed_at": NOW,
    }
    return PriceHistory(**(values | overrides))


def test_migrations_match_models(connection):
    diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_price_history_is_append_only(db):
    search = _search(db)
    db.add(_price_row(search.id, current_revision_id(db, search)))
    db.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(update(PriceHistory).values(price=Decimal("1.00")))
    db.rollback()


def test_search_revisions_are_append_only(db):
    _search(db)
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(update(SearchRevision).values(snapshot={}))
    db.rollback()


def test_duplicate_observation_rejected_even_with_null_return_date(db):
    search = _search(db)
    revision_id = current_revision_id(db, search)
    db.add(_price_row(search.id, revision_id))
    db.flush()
    db.add(_price_row(search.id, revision_id))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_location_needs_exactly_one_target(db):
    search = _search(db)
    db.add(
        SearchLocation(search_id=search.id, role="origin", airport_code="ZRH", country_code="CH")
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_trigger_function_exists(connection):
    count = connection.execute(
        text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_%_append_only'")
    ).scalar_one()
    assert count == 3  # price_history, search_revisions, query_log


def test_passenger_backfill_takes_passengers_from_the_revision(connection):
    """Migration 0010 fills `adults`/`children` of existing prices from their revision."""
    config = _alembic_config(connection)
    command.downgrade(config, "0009")
    search_id = connection.execute(
        text(
            "INSERT INTO searches (name, status, filters, poll_interval_minutes, revision_no) "
            "VALUES ('Old', 'active', '{}', 60, 1) RETURNING id"
        )
    ).scalar_one()
    revisions = [
        ('{"filters": {"adults": 2, "children": 1}}', 1),
        ('{"adults": 3}', 2),  # imported Suchabos: the snapshot is the filters
    ]
    for snapshot, number in revisions:
        revision_id = connection.execute(
            text(
                "INSERT INTO search_revisions (search_id, revision_no, snapshot) "
                "VALUES (:search, :no, CAST(:snapshot AS jsonb)) RETURNING id"
            ),
            {"search": search_id, "no": number, "snapshot": snapshot},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO price_history (search_id, search_revision_id, source, provider, "
                "origin_iata, destination_iata, departure_date, cabin_class, price, currency, "
                "observed_at) VALUES (:search, :revision, 'live', 'mock', 'ZRH', 'BCN', "
                "'2026-11-01', 'economy', 100, 'CHF', now() + make_interval(secs => :no))"
            ),
            {"search": search_id, "revision": revision_id, "no": number},
        )
    command.upgrade(config, "head")
    rows = connection.execute(
        text("SELECT adults, children FROM price_history ORDER BY search_revision_id")
    ).all()
    assert [tuple(row) for row in rows] == [(2, 1), (3, 0)]
