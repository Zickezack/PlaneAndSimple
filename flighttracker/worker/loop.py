"""Background worker: schedules polls and processes fetch jobs.

Run with `python -m flighttracker.worker`. Designed for a single worker process.
"""

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from flighttracker.config import Settings, get_settings
from flighttracker.db.session import create_db_engine, create_session_factory
from flighttracker.models import FetchJob, JobStatus
from flighttracker.providers.base import FlightPriceProvider, ProviderError
from flighttracker.providers.registry import get_provider, provider_config
from flighttracker.services.ingestion import (
    all_queries_failed,
    finish_job,
    plan_job,
    run_calendar_query,
    run_query,
)
from flighttracker.services.jobs import (
    claim_next_job,
    mark_done,
    mark_failed,
    refresh_status,
    requeue_running_jobs,
    requeue_stale_jobs,
    schedule_due_polls,
    schedule_due_trip_polls,
)
from flighttracker.services.settings import effective_settings
from flighttracker.worker.job_control import interrupt, should_end
from flighttracker.worker.trip_job import process_trip_job
from flighttracker.worker.wake import WakeListener

log = logging.getLogger("flighttracker.worker")

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
                process_trip_job(session, job, provider, clock, should_stop, currency)
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
                if should_end(session, job, should_stop):
                    interrupt(session, job)
                    log.info("Job %d %s after %d queries", job_id, job.status, len(entries))
                    return True
                entries.append(run_query(session, plan, query, provider, now=clock()))
                session.commit()
            # Price calendar (heat map) after the fares, so a failure here never delays them.
            for query in plan.calendar_queries if plan else []:
                if should_end(session, job, should_stop):
                    interrupt(session, job)
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
