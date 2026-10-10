"""Background worker: schedules polls and processes fetch jobs.

Run with `python -m flighttracker.worker`. Designed for a single worker process.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from flighttracker.config import Settings, get_settings
from flighttracker.db.session import create_db_engine, create_session_factory
from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.domain.spec import route_pairs
from flighttracker.domain.trips import LayoverRule, TripLegQuote, arrival_at, departure_at
from flighttracker.models import FetchJob, JobStatus, SearchRevision, SearchStatus
from flighttracker.providers.base import FlightPriceProvider, PriceQuery, ProviderError
from flighttracker.providers.registry import get_provider, provider_config
from flighttracker.services.ingestion import (
    _accept,
    all_queries_failed,
    finish_job,
    plan_job,
    run_calendar_query,
    run_query,
)
from flighttracker.services.jobs import (
    WAKE_CHANNEL,
    claim_next_job,
    mark_done,
    mark_failed,
    refresh_status,
    release_job,
    requeue_running_jobs,
    requeue_stale_jobs,
    schedule_due_polls,
    schedule_due_trip_polls,
)
from flighttracker.services.searches import current_revision_id, spec_of
from flighttracker.services.settings import effective_settings
from flighttracker.services.trips import (
    connect_quotes,
    get_trip,
    next_leg_dates,
    store_trip_quotes,
)

log = logging.getLogger("flighttracker.worker")

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _month_groups(days: tuple[date, ...]) -> dict[date, tuple[date, ...]]:
    grouped: dict[date, list[date]] = {}
    for day in days:
        grouped.setdefault(day.replace(day=1), []).append(day)
    return {month: tuple(values) for month, values in grouped.items()}


def _trip_query(
    search,
    cabin: CabinClass,
    month: date,
    days: tuple[date, ...],
    origin: str,
    destination: str,
    currency: str | None = None,
) -> PriceQuery:
    filters = SearchFilters.model_validate(search.filters)
    return PriceQuery(
        origin=origin,
        destination=destination,
        departure_month=month,
        cabin_class=cabin,
        trip_type=filters.trip_type,
        max_stops=filters.max_stops,
        stay_days_min=None,
        stay_days_max=None,
        adults=filters.adults,
        children=filters.children,
        currency=currency or filters.currency,
        days_per_month=1,
        departure_dates=days,
    )


def _process_trip_job(
    session: Session,
    job: FetchJob,
    provider: FlightPriceProvider,
    clock: Clock,
    should_stop: Callable[[], bool],
    currency: str | None = None,
) -> None:
    trip = get_trip(session, job.trip_id)
    if trip is None or trip.is_archived or trip.status is not SearchStatus.ACTIVE:
        mark_done(job, 0, clock())
        session.commit()
        return
    if not provider.supports_connection_times:
        mark_failed(
            job, "The active provider does not provide flight times for Trip connections", clock()
        )
        session.commit()
        return

    cabins = sorted(SearchFilters.model_validate(trip.legs[0].search.filters).cabin_classes)
    queries_total = queries_failed = quotes_stored = 0
    query_errors: list[str] = []
    now = clock()
    first_day = max(trip.starts_on, now.date() + timedelta(days=1))
    first_dates = tuple(
        first_day + timedelta(days=offset)
        for offset in range(max(0, (trip.ends_on - first_day).days + 1))
    )

    for cabin in cabins:
        paths: list[tuple[TripLegQuote, ...]] = []
        for position, leg in enumerate(trip.legs):
            if _should_end(session, job, should_stop):
                _interrupt(session, job)
                return
            if position == 0:
                departure_days = first_dates
            else:
                departure_days = next_leg_dates(trip, paths, leg, now.date())
            following_paths: list[tuple[TripLegQuote, ...]] = []
            for origin, destination in route_pairs(spec_of(leg.search)):
                for month, days in _month_groups(departure_days).items():
                    if _should_end(session, job, should_stop):
                        _interrupt(session, job)
                        return
                    query = _trip_query(
                        leg.search, cabin, month, days, origin, destination, currency
                    )
                    started_at = clock()
                    monotonic_started = time.monotonic()
                    error = None
                    try:
                        quotes = [
                            quote
                            for quote in provider.fetch_current(query)
                            if _accept(quote, query)
                            and trip.starts_on <= quote.departure_date <= trip.ends_on
                            and (arrival_at(quote) is not None)
                        ]
                    except ProviderError as exc:
                        quotes = []
                        error = str(exc)
                        query_errors.append(error)
                        queries_failed += 1
                    else:
                        if position == 0:
                            quotes = [
                                quote
                                for quote in quotes
                                if departure_at(quote) is not None
                                and arrival_at(quote).date() <= trip.ends_on
                            ]
                            following_paths.extend((quote,) for quote in quotes)
                        else:
                            rule = LayoverRule(leg.min_layover_days, leg.max_layover_days)
                            quotes, connected_paths = connect_quotes(
                                paths,
                                quotes,
                                rule,
                                final_leg=position == len(trip.legs) - 1,
                                ends_on=trip.ends_on,
                            )
                            following_paths.extend(connected_paths)
                    entry = store_trip_quotes(
                        session,
                        job=job,
                        search=leg.search,
                        revision=session.get(
                            SearchRevision, current_revision_id(session, leg.search)
                        ),
                        cabin_class=cabin,
                        departure_month=month,
                        provider_name=provider.name,
                        origin=origin,
                        destination=destination,
                        quotes=quotes,
                        adults=query.adults,
                        children=query.children,
                        error=error,
                        started_at=started_at,
                        duration_ms=int((time.monotonic() - monotonic_started) * 1000),
                    )
                    quotes_stored += entry.quotes_stored
                    queries_total += 1
                    session.commit()
            paths = following_paths
            if not paths:
                break

    refresh_status(session, job, lock=True)
    job.queries_total = queries_total
    job.queries_failed = queries_failed
    job.quotes_stored = quotes_stored
    completed_at = clock()
    trip.last_polled_at = completed_at
    for leg in trip.legs:
        leg.search.last_polled_at = completed_at
    if queries_total and queries_failed == queries_total:
        mark_failed(
            job, query_errors[-1] if query_errors else "All Trip queries failed", completed_at
        )
    else:
        mark_done(
            job, quotes_stored, completed_at, warning=query_errors[-1] if query_errors else None
        )
    session.commit()


def _should_end(session: Session, job: FetchJob, should_stop: Callable[[], bool]) -> bool:
    """Checked between two provider queries: worker stopping, or the poll cancelled in the UI."""
    return should_stop() or refresh_status(session, job) is JobStatus.CANCELLED


def _interrupt(session: Session, job: FetchJob) -> None:
    """A stopped worker hands the job back to the queue; a cancelled job stays cancelled.
    Everything fetched so far is already stored and logged."""
    refresh_status(session, job, lock=True)
    release_job(job)
    session.commit()


def schedule(
    session_factory: sessionmaker[Session],
    clock: Clock = _utcnow,
    base_settings: Settings | None = None,
) -> None:
    with session_factory() as session:
        now = clock()
        effective = effective_settings(session, base_settings) if base_settings else None
        interval = effective.default_poll_interval_minutes if effective else None
        requeued = requeue_stale_jobs(session, now)
        scheduled = schedule_due_polls(session, now, poll_interval_minutes=interval)
        trips_scheduled = schedule_due_trip_polls(session, now, poll_interval_minutes=interval)
        session.commit()
    if requeued or scheduled or trips_scheduled:
        log.info(
            "%d search polls and %d Trip polls scheduled, %d stale jobs requeued",
            scheduled,
            trips_scheduled,
            requeued,
        )


def process_next_job(
    session_factory: sessionmaker[Session],
    provider: FlightPriceProvider,
    clock: Clock = _utcnow,
    should_stop: Callable[[], bool] = lambda: False,
    base_settings: Settings | None = None,
) -> bool:
    """Claim and run one job, committing after every query. Returns False when the queue is empty.

    A stop request between two queries hands the job back to the queue, a cancellation from
    the Poll Log ends it; everything fetched so far is already stored and logged.
    """
    with session_factory() as session:
        job = claim_next_job(session, clock())
        if job is None:
            return False
        session.commit()
        job_id = job.id
        try:
            effective = effective_settings(session, base_settings) if base_settings else None
            currency = effective.default_currency if effective else None
            if job.trip_id is not None:
                _process_trip_job(session, job, provider, clock, should_stop, currency)
                log.info("Trip job %d completed", job_id)
                return True
            plan = plan_job(
                session,
                job,
                clock(),
                currency=currency,
                calendar=provider.calendar_days_per_request is not None,
            )
            entries = []
            for query in plan.queries if plan else []:
                if _should_end(session, job, should_stop):
                    _interrupt(session, job)
                    log.info("Job %d %s after %d queries", job_id, job.status, len(entries))
                    return True
                entries.append(run_query(session, plan, query, provider, now=clock()))
                session.commit()
            # Price calendar (heat map) after the fares, so a failure here never delays them.
            for query in plan.calendar_queries if plan else []:
                if _should_end(session, job, should_stop):
                    _interrupt(session, job)
                    log.info("Job %d %s during the price calendar", job_id, job.status)
                    return True
                run_calendar_query(session, plan, query, provider, now=clock())
                session.commit()
            refresh_status(session, job, lock=True)
            error = finish_job(session, job, entries, clock())
            if job.status is JobStatus.CANCELLED:
                log.info("Job %d cancelled after %d queries", job_id, len(entries))
            elif all_queries_failed(job):
                mark_failed(job, error or "All queries failed", clock())
                log.warning("Job %d: all %d queries failed: %s", job_id, len(entries), error)
            else:
                mark_done(job, job.quotes_stored, clock(), warning=error)
                log.info(
                    "Job %d (%s) done: %d queries (%d failed), %d new prices",
                    job_id,
                    job.kind,
                    job.queries_total,
                    job.queries_failed,
                    job.quotes_stored,
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(FetchJob, job_id)
            refresh_status(session, job, lock=True)
            # ProviderError messages are written to be safe; anything else only by type name
            # so that unexpected exceptions cannot leak secrets into the database.
            message = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            mark_failed(job, message, clock())
            session.commit()
            log.exception("Job %d failed", job_id)
    return True


class WakeListener:
    """Waits for `NOTIFY fetch_jobs` (sent when a Suchabo needs polling), at most `seconds`.

    Falls back to plain sleeping if the listening connection is unavailable.
    """

    def __init__(self, database_url: str, stop: threading.Event):
        # SQLAlchemy URL → libpq URL for a plain psycopg connection.
        url = make_url(database_url).set(drivername="postgresql")
        self._conninfo = url.render_as_string(hide_password=False)
        self._stop = stop
        self._connection: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection | None:
        if self._connection is None or self._connection.closed:
            try:
                self._connection = psycopg.connect(self._conninfo, autocommit=True)
                self._connection.execute(f"LISTEN {WAKE_CHANNEL}")
            except psycopg.Error:
                log.warning("Cannot listen for wake-ups, polling every tick instead")
                self._connection = None
        return self._connection

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
            connection = self._connect()
            if connection is None:
                self._stop.wait(remaining)
                return
            try:
                # Short slices so that a stop request is noticed within a second.
                if list(connection.notifies(timeout=min(1.0, remaining), stop_after=1)):
                    return
            except psycopg.Error:
                self._connection = None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()


def refresh_provider(
    provider: FlightPriceProvider, config: tuple, effective: Settings
) -> tuple[FlightPriceProvider, tuple]:
    """Rebuilds the provider when a provider setting changed in Platform Settings.

    An unusable combination (e.g. Travelpayouts without a token) keeps the previous provider
    instead of stopping the worker, which would otherwise crash again on every restart.
    """
    new_config = provider_config(effective)
    if new_config == config:
        return provider, config
    try:
        provider = get_provider(effective)
    except ValueError as exc:
        log.error("Provider settings not applied, still using %s: %s", provider.name, exc)
    else:
        log.info("Flight data provider set up from Platform Settings: %s", provider.name)
    return provider, new_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    session_factory = create_session_factory(create_db_engine(settings.database_url))
    with session_factory() as session:
        effective = effective_settings(session, settings)
    # `.env` itself must be usable (fail fast); stored overrides are applied on top.
    provider, config = refresh_provider(
        get_provider(settings), provider_config(settings), effective
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with session_factory() as session:
        requeued = requeue_running_jobs(session, _utcnow())
        session.commit()
    log.info("Worker started (provider: %s, %d interrupted jobs requeued)", provider.name, requeued)
    listener = WakeListener(settings.database_url, stop)
    while not stop.is_set():
        with session_factory() as session:
            effective = effective_settings(session, settings)
        provider, config = refresh_provider(provider, config, effective)
        try:
            schedule(session_factory, base_settings=settings)
            while not stop.is_set() and process_next_job(
                session_factory, provider, should_stop=stop.is_set, base_settings=settings
            ):
                pass
        except Exception:
            log.exception("Worker tick failed")
        listener.wait(effective.worker_tick_seconds)
    listener.close()
    log.info("Worker stopped")


if __name__ == "__main__":
    main()
