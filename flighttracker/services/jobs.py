from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, exists, func, select, text, update
from sqlalchemy.orm import Session

from flighttracker.models import (
    FetchJob,
    JobKind,
    JobStatus,
    QueryLog,
    Search,
    SearchStatus,
    Trip,
    TripLeg,
)

MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=5)
STALE_JOB_TIMEOUT = timedelta(hours=1)
MAX_ERROR_LENGTH = 2000
# PostgreSQL channel the worker listens on, so new work starts without waiting for the next tick.
WAKE_CHANNEL = "fetch_jobs"


def wake_worker(session: Session) -> None:
    """Delivered by PostgreSQL only when the surrounding transaction commits."""
    session.execute(text(f"NOTIFY {WAKE_CHANNEL}"))


def enqueue_job(
    session: Session,
    search_id: int,
    kind: JobKind,
    *,
    run_after: datetime | None = None,
    trip_id: int | None = None,
) -> FetchJob:
    job = FetchJob(search_id=search_id, trip_id=trip_id, kind=kind, status=JobStatus.QUEUED)
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    session.flush()
    return job


def has_open_poll(session: Session, search_id: int) -> bool:
    return session.scalar(
        select(
            exists().where(
                FetchJob.search_id == search_id,
                FetchJob.kind == JobKind.POLL,
                FetchJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    )


def _lock_row(session: Session, model: type[Search] | type[Trip], row_id: int) -> None:
    """Row lock until commit. The scheduler selects with SKIP LOCKED, so it leaves a Suchabo or
    Trip alone while "poll now" checks for an open poll – and "poll now" waits for a scheduler
    that got there first. Either way only one job is queued."""
    session.execute(select(model.id).where(model.id == row_id).with_for_update())


def request_poll(
    session: Session,
    search: Search,
    now: datetime,
    *,
    poll_interval_minutes: int | None = None,
) -> bool:
    """Poll a Suchabo right away ("poll now"). New prices are added – the history is
    append-only, so nothing is overwritten; the latest poll simply becomes the current price.

    Returns False when a poll is already queued or running.
    """
    _lock_row(session, Search, search.id)
    if has_open_poll(session, search.id):
        return False
    enqueue_job(session, search.id, JobKind.POLL, run_after=now)
    interval = poll_interval_minutes or search.poll_interval_minutes
    search.next_poll_at = now + timedelta(minutes=interval)
    wake_worker(session)
    return True


def schedule_due_polls(
    session: Session, now: datetime, *, poll_interval_minutes: int | None = None
) -> int:
    """Queue a poll job for every active Suchabo whose `next_poll_at` has passed."""
    open_poll = exists().where(
        FetchJob.search_id == Search.id,
        FetchJob.kind == JobKind.POLL,
        FetchJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
    )
    trip_leg = exists().where(TripLeg.search_id == Search.id)
    due = session.scalars(
        select(Search)
        .where(
            Search.status == SearchStatus.ACTIVE,
            Search.archived_at.is_(None),
            Search.next_poll_at <= now,
            ~open_poll,
            ~trip_leg,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for search in due:
        enqueue_job(session, search.id, JobKind.POLL, run_after=now)
        interval = poll_interval_minutes or search.poll_interval_minutes
        search.next_poll_at = now + timedelta(minutes=interval)
    return len(due)


def schedule_due_trip_polls(
    session: Session, now: datetime, *, poll_interval_minutes: int | None = None
) -> int:
    """Queue one coordinated poll for each due Trip, anchored to its first leg."""
    open_poll = exists().where(
        FetchJob.trip_id == Trip.id,
        FetchJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
    )
    due = session.scalars(
        select(Trip)
        .where(
            Trip.status == SearchStatus.ACTIVE,
            Trip.archived_at.is_(None),
            Trip.ends_on > now.date(),
            Trip.next_poll_at <= now,
            ~open_poll,
        )
        .with_for_update(skip_locked=True)
    ).all()
    queued = 0
    for trip in due:
        first_leg = session.scalar(
            select(TripLeg).where(TripLeg.trip_id == trip.id).order_by(TripLeg.position).limit(1)
        )
        if first_leg is None:
            continue
        enqueue_job(session, first_leg.search_id, JobKind.POLL, run_after=now, trip_id=trip.id)
        interval = poll_interval_minutes or trip.poll_interval_minutes
        trip.next_poll_at = now + timedelta(minutes=interval)
        queued += 1
    return queued


def request_trip_poll(
    session: Session,
    trip: Trip,
    now: datetime,
    *,
    poll_interval_minutes: int | None = None,
) -> bool:
    """Queue a Trip poll now unless the same Trip already has an open job."""
    _lock_row(session, Trip, trip.id)
    if session.scalar(
        select(
            exists().where(
                FetchJob.trip_id == trip.id,
                FetchJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    ):
        return False
    first_leg = session.scalar(
        select(TripLeg).where(TripLeg.trip_id == trip.id).order_by(TripLeg.position).limit(1)
    )
    if first_leg is None:
        return False
    enqueue_job(session, first_leg.search_id, JobKind.POLL, run_after=now, trip_id=trip.id)
    interval = poll_interval_minutes or trip.poll_interval_minutes
    trip.next_poll_at = now + timedelta(minutes=interval)
    wake_worker(session)
    return True


def claim_next_job(session: Session, now: datetime) -> FetchJob | None:
    job = session.scalars(
        select(FetchJob)
        .where(FetchJob.status == JobStatus.QUEUED, FetchJob.run_after <= now)
        .order_by(FetchJob.run_after, FetchJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.started_at = now
    job.attempts += 1
    return job


def mark_done(job: FetchJob, quotes_stored: int, now: datetime, warning: str | None = None) -> None:
    """`warning`: error of single failed queries while the rest succeeded."""
    job.quotes_stored = quotes_stored
    if job.status is JobStatus.CANCELLED:
        return
    job.status = JobStatus.DONE
    job.quotes_stored = quotes_stored
    job.finished_at = now
    job.last_error = warning[:MAX_ERROR_LENGTH] if warning else None


def release_job(job: FetchJob) -> None:
    """Hand an interrupted job back to the queue without counting the attempt."""
    if job.status is JobStatus.CANCELLED:
        return
    job.status = JobStatus.QUEUED
    job.attempts = max(job.attempts - 1, 0)
    job.started_at = None


def mark_failed(job: FetchJob, error: str, now: datetime) -> None:
    if job.status is JobStatus.CANCELLED:
        return
    job.last_error = error[:MAX_ERROR_LENGTH]
    if job.attempts < MAX_ATTEMPTS:
        job.status = JobStatus.QUEUED
        job.run_after = now + RETRY_BACKOFF * job.attempts
    else:
        job.status = JobStatus.FAILED
        job.finished_at = now


def requeue_running_jobs(session: Session, now: datetime) -> int:
    """At worker start: every `running` job was left behind by a stopped or killed worker.

    The interrupted attempt is not counted – a restart is not the job's fault. Assumes a
    single worker (see docker-compose.yml); with several, rely on `requeue_stale_jobs` only.
    """
    result = session.execute(
        update(FetchJob)
        .where(FetchJob.status == JobStatus.RUNNING)
        .values(
            status=JobStatus.QUEUED,
            run_after=now,
            started_at=None,
            attempts=func.greatest(FetchJob.attempts - 1, 0),
        )
    )
    return result.rowcount


def requeue_stale_jobs(session: Session, now: datetime) -> int:
    """Jobs left `running` by a crashed worker go back into the queue."""
    result = session.execute(
        update(FetchJob)
        .where(FetchJob.status == JobStatus.RUNNING, FetchJob.started_at < now - STALE_JOB_TIMEOUT)
        .values(status=JobStatus.QUEUED, run_after=now)
    )
    return result.rowcount


def recent_jobs(session: Session, search_id: int, limit: int = 10) -> list[FetchJob]:
    return list(
        session.scalars(
            select(FetchJob)
            .where(FetchJob.search_id == search_id)
            .order_by(FetchJob.created_at.desc(), FetchJob.id.desc())
            .limit(limit)
        )
    )


OPEN_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)
CANCELLED_MESSAGE = "Cancelled"


def cancel_job(session: Session, job_id: int, now: datetime) -> bool:
    """Cancel a queued or running poll. A running one stops after its current provider query;
    what it fetched so far stays stored. False when the job is no longer open."""
    job = session.scalar(select(FetchJob).where(FetchJob.id == job_id).with_for_update())
    if job is None or job.status not in OPEN_STATUSES:
        return False
    job.status = JobStatus.CANCELLED
    job.finished_at = now
    job.last_error = CANCELLED_MESSAGE
    return True


def refresh_status(session: Session, job: FetchJob, *, lock: bool = False) -> JobStatus:
    """The job's status as stored now – the web may have cancelled it meanwhile.

    `lock`: keep the row locked until commit, so a cancellation cannot slip in between this
    check and the worker's final status change.
    """
    session.refresh(job, attribute_names=["status"], with_for_update=lock or None)
    return job.status


@dataclass(frozen=True)
class OpenJob:
    job: FetchJob
    name: str
    trip_id: int | None
    queries_done: int


def open_jobs(session: Session, visible: ColumnElement[bool] | None = None) -> list[OpenJob]:
    """Queued and running polls, running first. `visible`: condition on `Search`."""
    queries_done = (
        select(func.count())
        .where(QueryLog.job_id == FetchJob.id)
        .correlate(FetchJob)
        .scalar_subquery()
    )
    statement = (
        select(FetchJob, Search.name, Trip.name, queries_done)
        .join(Search, Search.id == FetchJob.search_id)
        .outerjoin(Trip, Trip.id == FetchJob.trip_id)
        .where(FetchJob.status.in_(OPEN_STATUSES))
        .order_by(FetchJob.status.desc(), FetchJob.run_after, FetchJob.id)
    )
    if visible is not None:
        statement = statement.where(visible)
    return [
        OpenJob(job, trip_name or search_name, job.trip_id, done)
        for job, search_name, trip_name, done in session.execute(statement)
    ]
