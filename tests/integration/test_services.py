from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from flighttracker.domain.coverage import CoverageKind
from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.domain.locations import LocationRef
from flighttracker.domain.spec import SearchSpec
from flighttracker.models import (
    Airport,
    FetchJob,
    JobKind,
    JobStatus,
    PriceHistory,
    PriceSource,
    QueryLog,
    QueryOutcome,
    Search,
    SearchLocation,
    SearchRevision,
)
from flighttracker.providers.base import FlightPriceProvider, ProviderError
from flighttracker.providers.mock import MockProvider
from flighttracker.services import history, jobs
from flighttracker.services.airport_import import import_needed, upsert_airports
from flighttracker.services.ingestion import run_fetch_job
from flighttracker.services.searches import (
    SearchInput,
    SearchValidationError,
    airport_options,
    archive_search,
    create_search,
    current_revision_id,
    delete_search_permanently,
    find_overlapping_searches,
    list_searches,
    merge_into,
    pause_search,
    restore_search,
    spec_of,
    suggest_locations,
    update_search,
    with_default_airports,
)
from flighttracker.services.trips import TripInput, TripLegInput, create_trip
from flighttracker.worker.__main__ import process_next_job, schedule

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
A = LocationRef.airport
C = LocationRef.country
# All selectable airports of the seeded countries (see conftest.py).
FULL_SELECTION = {"CH": {"ZRH", "GVA"}, "ES": {"BCN", "MAD", "PMI"}}


def make_input(origins, destinations, name="Test", airports=None, **filters) -> SearchInput:
    countries = {ref.code for ref in [*origins, *destinations] if ref.kind == "country"}
    selection = {
        code: frozenset(FULL_SELECTION[code]) for code in countries if code in FULL_SELECTION
    }
    selection |= {code: frozenset(codes) for code, codes in (airports or {}).items()}
    spec = SearchSpec(
        frozenset(origins), frozenset(destinations), SearchFilters(**filters), selection
    )
    return SearchInput(name, spec, 360)


def create(db, origins=(A("ZRH"),), destinations=(A("BCN"),), **filters) -> Search:
    return create_search(
        db, make_input(origins, destinations, **filters), max_route_pairs=50, now=NOW
    )


def run_all_jobs(db, provider, now=NOW) -> int:
    jobs.schedule_due_polls(db, now)
    stored = 0
    while (job := jobs.claim_next_job(db, now)) is not None:
        stored += run_fetch_job(db, job, provider, now=now)
        jobs.mark_done(job, stored, now)
        db.flush()
    return stored


def history_rows(db, search_id):
    return db.execute(
        select(
            PriceHistory.id,
            PriceHistory.price,
            PriceHistory.search_revision_id,
            PriceHistory.cabin_class,
            PriceHistory.observed_at,
        )
        .where(PriceHistory.search_id == search_id)
        .order_by(PriceHistory.id)
    ).all()


def count(db, model, search_id) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.search_id == search_id))


class TestCreate:
    def test_creates_locations_revision_and_polls_immediately(self, db):
        search = create(db, origins=[C("CH")], destinations=[A("BCN"), A("MAD")])
        assert spec_of(search).origins == {C("CH")}
        assert spec_of(search).country_airports == {"CH": {"ZRH", "GVA"}}
        revision = db.scalars(
            select(SearchRevision).where(SearchRevision.search_id == search.id)
        ).one()
        assert revision.revision_no == 1
        assert revision.snapshot["origins"] == [{"kind": "country", "code": "CH"}]
        assert revision.snapshot["country_airports"] == {"CH": ["GVA", "ZRH"]}
        assert search.next_poll_at == NOW
        assert jobs.schedule_due_polls(db, NOW) == 1

    def test_rejects_unknown_codes(self, db):
        with pytest.raises(SearchValidationError) as info:
            create(db, origins=[A("XXX")], destinations=[C("QQ")])
        assert any("XXX" in str(e) for e in info.value.errors)
        assert any("QQ" in str(e) for e in info.value.errors)

    def test_rejects_too_many_routes(self, db):
        with pytest.raises(SearchValidationError, match="Too many routes"):
            create_search(db, make_input([C("CH")], [C("ES")]), max_route_pairs=3, now=NOW)

    def test_country_queries_only_selected_airports(self, db):
        search = create_search(
            db,
            make_input([C("CH")], [A("BCN")], airports={"CH": {"ZRH"}}),
            max_route_pairs=50,
            now=NOW,
        )
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        origins = set(
            db.scalars(
                select(PriceHistory.origin_iata)
                .where(PriceHistory.search_id == search.id)
                .distinct()
            )
        )
        assert origins == {"ZRH"}

    @pytest.mark.parametrize(
        ("selection", "message"),
        [
            ({"CH": {"BCN"}}, "not a selectable airport"),  # airport of another country
            ({"CH": {"BRN"}}, "not a selectable airport"),  # no scheduled service
            ({"CH": set()}, "at least one airport"),
        ],
    )
    def test_rejects_invalid_airport_selection(self, db, selection, message):
        with pytest.raises(SearchValidationError, match=message):
            create_search(
                db,
                make_input([C("CH")], [A("BCN")], airports=selection),
                max_route_pairs=50,
                now=NOW,
            )


class TestAirportSelection:
    def test_options_are_ranked_by_passengers(self, db):
        db.get(Airport, "GVA").passengers = 18_000_000
        db.get(Airport, "ZRH").passengers = 32_000_000
        db.flush()
        options = airport_options(db, {"CH"})
        assert [o.iata_code for o in options["CH"]] == ["ZRH", "GVA"]

    def test_defaults_fill_only_unreviewed_countries(self, db):
        db.get(Airport, "MAD").passengers = 60_000_000
        db.get(Airport, "BCN").passengers = 50_000_000
        db.flush()
        spec = SearchSpec(
            frozenset({C("CH")}),
            frozenset({C("ES")}),
            SearchFilters(),
            {"CH": frozenset({"GVA"})},
        )
        completed = with_default_airports(spec, airport_options(db, {"ES"}), 2)
        assert completed.country_airports == {"CH": {"GVA"}, "ES": {"MAD", "BCN"}}

    def test_import_without_wikidata_keeps_passengers(self, db):
        row = {
            "iata_code": "ZRH",
            "name": "Zurich",
            "city": "Zürich",
            "country_code": "CH",
            "airport_type": "large_airport",
            "has_scheduled_service": True,
        }
        upsert_airports(db, [row], {"ZRH": 32_000_000})
        upsert_airports(db, [row | {"name": "Zürich Flughafen"}], {})
        db.expire_all()
        airport = db.get(Airport, "ZRH")
        assert (airport.name, airport.passengers) == ("Zürich Flughafen", 32_000_000)
        assert not import_needed(db)

    def test_import_is_only_repeated_when_passenger_numbers_can_arrive(self, db):
        db.execute(update(Airport).values(passengers=None))
        assert db.scalar(select(func.count()).select_from(Airport)) > 0
        assert not import_needed(db)  # no contact: missing numbers are expected
        assert import_needed(db, wikidata_contact="ops@example.org")

    def test_changing_selection_creates_revision(self, db):
        search = create(db, origins=[C("CH")], destinations=[A("BCN")])
        changed = update_search(
            db,
            search,
            make_input([C("CH")], [A("BCN")], airports={"CH": {"ZRH"}}),
            max_route_pairs=50,
            now=NOW,
        )
        db.flush()
        assert changed and search.revision_no == 2
        location = db.scalars(
            select(SearchLocation).where(
                SearchLocation.search_id == search.id, SearchLocation.country_code == "CH"
            )
        ).one()
        assert location.airport_codes == ["ZRH"]


class TestHistoryIsPreserved:
    def test_update_creates_revision_and_keeps_history(self, db):
        search = create(db)
        provider = MockProvider(clock=lambda: NOW)
        assert run_all_jobs(db, provider) > 0
        before = history_rows(db, search.id)

        changed = update_search(
            db,
            search,
            make_input([A("ZRH"), A("GVA")], [A("BCN")], cabin_classes={CabinClass.BUSINESS}),
            max_route_pairs=50,
            now=NOW,
        )
        db.flush()
        assert changed
        assert search.revision_no == 2
        assert history_rows(db, search.id)[: len(before)] == before

        # Broadened search → polled right away; new rows point to revision 2.
        assert search.next_poll_at == NOW
        run_all_jobs(db, provider, now=NOW + timedelta(hours=1))
        after = history_rows(db, search.id)
        assert len(after) > len(before)
        revision_ids = {row.search_revision_id for row in after[len(before) :]}
        assert len(revision_ids) == 1 and revision_ids != {before[0].search_revision_id}

    def test_update_without_changes_creates_no_revision(self, db):
        search = create(db)
        assert not update_search(
            db, search, make_input([A("ZRH")], [A("BCN")]), max_route_pairs=50, now=NOW
        )
        assert count(db, SearchRevision, search.id) == 1

    def test_narrowing_keeps_poll_schedule(self, db):
        search = create(db, max_stops=1)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        next_poll = search.next_poll_at
        update_search(
            db,
            search,
            make_input([A("ZRH")], [A("BCN")], max_stops=0),
            max_route_pairs=50,
            now=NOW + timedelta(hours=1),
        )
        assert search.next_poll_at == next_poll

    def test_archive_keeps_everything(self, db):
        search = create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        rows = count(db, PriceHistory, search.id)
        archive_search(search, NOW)
        db.flush()
        assert search not in list_searches(db)
        assert search in list_searches(db, archived=True)
        assert count(db, PriceHistory, search.id) == rows
        restore_search(search)
        db.flush()
        assert search in list_searches(db)

    def test_permanent_delete_cascades(self, db):
        search = create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        search_id = search.id
        delete_search_permanently(db, search)
        db.flush()
        for model in (PriceHistory, SearchRevision, SearchLocation, FetchJob):
            assert count(db, model, search_id) == 0


class TestOverlaps:
    def test_detects_covered_and_mergeable(self, db):
        country_search = create(db, origins=[C("CH")], destinations=[C("ES")])
        airport_search = create(db, origins=[A("GVA")], destinations=[A("MAD")])
        new_spec = make_input([A("ZRH")], [A("BCN")]).spec
        matches = {s.id: kind for s, kind in find_overlapping_searches(db, new_spec)}
        assert matches == {
            country_search.id: CoverageKind.COVERED,
            airport_search.id: CoverageKind.MERGEABLE,
        }

    def test_ignores_archived(self, db):
        search = create(db, origins=[C("CH")], destinations=[C("ES")])
        archive_search(search, NOW)
        db.flush()
        assert find_overlapping_searches(db, make_input([A("ZRH")], [A("BCN")]).spec) == []

    def test_merge_unions_locations_and_filters(self, db):
        target = create(db, max_stops=0)
        new_spec = make_input(
            [A("GVA")], [A("MAD")], cabin_classes={CabinClass.BUSINESS}, max_stops=1
        ).spec
        merge_into(db, target, new_spec, max_route_pairs=50, now=NOW)
        merged = spec_of(target)
        assert merged.origins == {A("ZRH"), A("GVA")}
        assert merged.destinations == {A("BCN"), A("MAD")}
        assert merged.filters.cabin_classes == {CabinClass.ECONOMY, CabinClass.BUSINESS}
        assert merged.filters.max_stops == 1
        assert target.revision_no == 2


class TestJobs:
    def test_schedules_due_polls_once(self, db):
        active = create(db)
        paused = create(db, origins=[A("GVA")])
        pause_search(paused)
        db.flush()
        assert jobs.schedule_due_polls(db, NOW) == 1
        assert jobs.schedule_due_polls(db, NOW) == 0
        assert active.next_poll_at == NOW + timedelta(minutes=active.poll_interval_minutes)

    def test_global_interval_is_used_for_search_and_trip_schedulers(self, db):
        search = create(db)
        starts_on = NOW.date() + timedelta(days=14)
        trip = create_trip(
            db,
            TripInput(
                name="Timed loop",
                starts_on=starts_on,
                ends_on=starts_on + timedelta(days=7),
                poll_interval_minutes=360,
                legs=(TripLegInput("ZRH", "BCN"), TripLegInput("BCN", "ZRH")),
                cabin_classes=frozenset({CabinClass.ECONOMY}),
                max_stops=None,
                adults=1,
                currency="CHF",
            ),
            max_route_pairs=50,
        )
        trip.next_poll_at = NOW
        assert jobs.schedule_due_polls(db, NOW, poll_interval_minutes=1440) == 1
        assert jobs.schedule_due_trip_polls(db, NOW, poll_interval_minutes=1440) == 1
        assert search.next_poll_at == NOW + timedelta(days=1)
        assert trip.next_poll_at == NOW + timedelta(days=1)

    def test_retry_then_fail(self, db):
        search = create(db)
        jobs.schedule_due_polls(db, NOW)
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            claimed = jobs.claim_next_job(db, NOW + timedelta(days=attempt))
            assert claimed.id == job.id and claimed.attempts == attempt
            jobs.mark_failed(claimed, "boom", NOW + timedelta(days=attempt))
            db.flush()
        assert job.status is JobStatus.FAILED
        assert job.last_error == "boom"

    def test_requeues_stale_jobs(self, db):
        create(db)
        jobs.schedule_due_polls(db, NOW)
        job = jobs.claim_next_job(db, NOW)
        db.flush()
        assert jobs.requeue_stale_jobs(db, NOW + timedelta(hours=2)) == 1
        db.refresh(job)
        assert job.status is JobStatus.QUEUED

    def test_poll_skips_paused_search(self, db):
        search = create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        pause_search(search)
        jobs.enqueue_job(db, search.id, JobKind.POLL, run_after=NOW)
        assert run_all_jobs(db, MockProvider(clock=lambda: NOW)) == 0


class _FailingProvider(FlightPriceProvider):
    name = "failing"

    def fetch_current(self, query):
        raise ProviderError("Quelle nicht erreichbar")


class TestWorker:
    def test_first_poll_end_to_end(self, db, session_factory):
        search = create(db)
        db.commit()
        provider = MockProvider(clock=lambda: NOW)
        schedule(session_factory, clock=lambda: NOW)
        assert process_next_job(session_factory, provider, clock=lambda: NOW)
        assert not process_next_job(session_factory, provider, clock=lambda: NOW)

        sources = set(
            db.scalars(
                select(PriceHistory.source).where(PriceHistory.search_id == search.id).distinct()
            )
        )
        assert sources == {PriceSource.LIVE}
        overview = history.monthly_overview(db, search.id, NOW.date())
        assert overview
        # Previous-year values only appear once our own tracking is a year old.
        assert all(row.previous_year is None for row in overview)

    def test_worker_uses_global_currency_and_poll_interval(self, db, session_factory):
        from flighttracker.config import Settings
        from flighttracker.services.settings import set_override

        search = create(db)
        set_override(db, "default_currency", "EUR")
        set_override(db, "default_poll_interval_minutes", "1440")
        db.commit()
        base_settings = Settings(
            _env_file=None,
            database_url="postgresql+psycopg://unused/unused_test",
            secret_key="x" * 40,
            admin_username="admin",
            admin_password_hash="unused",
        )

        schedule(session_factory, clock=lambda: NOW, base_settings=base_settings)
        db.refresh(search)
        assert search.next_poll_at == NOW + timedelta(days=1)
        assert process_next_job(
            session_factory,
            MockProvider(clock=lambda: NOW),
            clock=lambda: NOW,
            base_settings=base_settings,
        )
        currencies = set(
            db.scalars(select(PriceHistory.currency).where(PriceHistory.search_id == search.id))
        )
        assert currencies == {"EUR"}

    def test_failed_job_is_retried_with_safe_error(self, db, session_factory):
        search = create(db)
        db.commit()
        schedule(session_factory, clock=lambda: NOW)
        assert process_next_job(session_factory, _FailingProvider(), clock=lambda: NOW)
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        db.refresh(job)
        assert job.status is JobStatus.QUEUED
        assert job.last_error == "Quelle nicht erreichbar"


class TestSuggestions:
    def test_exact_code_first_then_by_size(self, db):
        madrid, palma = db.get(Airport, "MAD"), db.get(Airport, "PMI")
        madrid.name, madrid.passengers = "Madrid-Barajas", 60_000_000
        palma.name, palma.passengers = "Palma de Mallorca", 30_000_000
        db.flush()
        assert suggest_locations(db, "es")[0].code == "ES"
        assert [s.code for s in suggest_locations(db, "ma")] == ["MAD", "PMI"]
        assert suggest_locations(db, "pmi")[0].code == "PMI"

    def test_skips_airports_without_scheduled_service(self, db):
        assert "BRN" not in {s.code for s in suggest_locations(db, "BRN")}
        assert suggest_locations(db, "  ") == []


class TestFakeData:
    def test_overview_flags_prices_of_fake_providers(self, db):
        search = create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        flagged = history.monthly_overview(db, search.id, NOW.date(), {"mock"})
        assert flagged and all(row.fake for row in flagged)
        unflagged = history.monthly_overview(db, search.id, NOW.date())
        assert not any(row.fake for row in unflagged)


class _FlakyProvider(FlightPriceProvider):
    """Fails for one destination, has no flights for another, prices for the rest."""

    name = "flaky"

    def __init__(self):
        self._mock = MockProvider(clock=lambda: NOW)

    def fetch_current(self, query):
        if query.destination == "MAD":
            raise ProviderError("Madrid unreachable")
        if query.destination == "PMI":
            return []
        return self._mock.fetch_current(query)


class TestQueryLog:
    def test_every_query_is_logged_and_partial_failures_do_not_fail_the_job(
        self, db, session_factory
    ):
        search = create(db, destinations=[A("BCN"), A("MAD"), A("PMI")], months_ahead=2)
        db.commit()
        schedule(session_factory, clock=lambda: NOW)
        assert process_next_job(session_factory, _FlakyProvider(), clock=lambda: NOW)

        entries = db.scalars(select(QueryLog).where(QueryLog.search_id == search.id)).all()
        outcomes = {(e.destination_iata, e.departure_month.month, e.outcome) for e in entries}
        # September (NOW's month) has no future departure days left, October has prices.
        assert outcomes == {
            ("BCN", 9, QueryOutcome.EMPTY),
            ("BCN", 10, QueryOutcome.OK),
            ("MAD", 9, QueryOutcome.FAILED),
            ("MAD", 10, QueryOutcome.FAILED),
            ("PMI", 9, QueryOutcome.EMPTY),
            ("PMI", 10, QueryOutcome.EMPTY),
        }
        assert len(entries) == 6  # 3 routes × 2 months
        assert all(e.search_name == "Test" and e.provider == "flaky" for e in entries)
        ok = next(e for e in entries if e.outcome is QueryOutcome.OK)
        assert len(ok.results) == ok.quotes_found and ok.results[0]["currency"] == "CHF"
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        db.refresh(job)
        assert (job.status, job.queries_total, job.queries_failed) == (JobStatus.DONE, 6, 2)
        assert job.last_error == "Madrid unreachable"
        assert job.quotes_stored == count(db, PriceHistory, search.id) > 0

    def test_stop_request_requeues_job_and_keeps_fetched_prices(self, db, session_factory):
        search = create(db, months_ahead=3)
        db.commit()
        schedule(session_factory, clock=lambda: NOW)
        answers = iter([False, False, True])  # stop after two queries (September, October)
        provider = MockProvider(clock=lambda: NOW)
        assert process_next_job(
            session_factory, provider, clock=lambda: NOW, should_stop=lambda: next(answers)
        )
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        db.refresh(job)
        assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)
        assert count(db, QueryLog, search.id) == 2
        stored_before = count(db, PriceHistory, search.id)
        assert stored_before > 0

        # Resumed: only the missing third month runs; counters cover both runs.
        assert process_next_job(session_factory, provider, clock=lambda: NOW)
        db.refresh(job)
        assert count(db, QueryLog, search.id) == 3
        assert (job.status, job.queries_total, job.queries_failed) == (JobStatus.DONE, 3, 0)
        assert job.quotes_stored == count(db, PriceHistory, search.id) > stored_before

    def test_log_survives_permanent_deletion(self, db):
        search = create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        search_id = search.id
        delete_search_permanently(db, search)
        db.flush()
        assert count(db, QueryLog, search_id) > 0

    def test_log_is_append_only(self, db):
        create(db)
        run_all_jobs(db, MockProvider(clock=lambda: NOW))
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(update(QueryLog).values(error="changed"))
        db.rollback()


class TestWorkerRestart:
    def test_running_jobs_are_requeued_at_start_without_counting_the_attempt(self, db):
        create(db)
        jobs.schedule_due_polls(db, NOW)
        job = jobs.claim_next_job(db, NOW)
        db.flush()
        assert job.attempts == 1
        assert jobs.requeue_running_jobs(db, NOW) == 1
        db.refresh(job)
        assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


class TestPricePoints:
    def _row(self, search, revision_id, **overrides):
        values = {
            "search_id": search.id,
            "search_revision_id": revision_id,
            "source": PriceSource.LIVE,
            "provider": "google_flights",
            "origin_iata": "ZRH",
            "destination_iata": "BCN",
            "departure_date": date(2026, 11, 3),
            "return_date": date(2026, 11, 10),
            "cabin_class": CabinClass.ECONOMY,
            "stops": 0,
            "price": Decimal("200"),
            "currency": "CHF",
            "observed_at": NOW,
        }
        return PriceHistory(**(values | overrides))

    def test_latest_poll_wins_even_when_the_stops_change(self, db):
        search = create(db)
        revision_id = current_revision_id(db, search)
        db.add_all(
            [
                # Yesterday the cheapest was a connecting flight for 150 …
                self._row(
                    search,
                    revision_id,
                    stops=1,
                    price=Decimal("150"),
                    observed_at=NOW - timedelta(days=1),
                ),
                # … today the cheapest is direct for 180: 180 is the current price, not 150.
                self._row(search, revision_id, stops=0, price=Decimal("180")),
            ]
        )
        db.flush()
        (point,) = history.price_points(db, search.id)
        assert (point.price, point.stops, point.previous_price) == (
            Decimal("180"),
            0,
            Decimal("150"),
        )

    def test_current_month_ignores_departures_in_the_past(self, db):
        search = create(db)
        revision_id = current_revision_id(db, search)
        db.add_all(
            [
                self._row(
                    search, revision_id, departure_date=date(2026, 9, 10), price=Decimal("50")
                ),
                self._row(
                    search, revision_id, departure_date=date(2026, 9, 30), price=Decimal("250")
                ),
            ]
        )
        db.flush()
        (row,) = history.overview_from_points(history.price_points(db, search.id), NOW.date())
        assert row.current.price == Decimal("250")
        assert row.current.point.departure_date == date(2026, 9, 30)


class TestPollNow:
    def test_poll_now_once_and_never_twice_in_parallel(self, db):
        search = create(db)
        jobs.schedule_due_polls(db, NOW)  # the first automatic poll is already queued
        assert not jobs.request_poll(db, search, NOW)
        job = jobs.claim_next_job(db, NOW)
        jobs.mark_done(job, 0, NOW)
        db.flush()
        assert jobs.request_poll(db, search, NOW + timedelta(minutes=5))
        assert search.next_poll_at == NOW + timedelta(minutes=5 + search.poll_interval_minutes)
        assert count(db, FetchJob, search.id) == 2
