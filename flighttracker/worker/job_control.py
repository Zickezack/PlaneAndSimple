"""Checks between two provider queries, shared by every job kind."""

from collections.abc import Callable

from sqlalchemy.orm import Session

from flighttracker.models import FetchJob, JobStatus
from flighttracker.services.jobs import (
    refresh_status,
    release_job,
)


def should_end(session: Session, job: FetchJob, should_stop: Callable[[], bool]) -> bool:
    """Checked between two provider queries: worker stopping, or the poll cancelled in the UI."""
    return should_stop() or refresh_status(session, job) is JobStatus.CANCELLED


def interrupt(session: Session, job: FetchJob) -> None:
    """A stopped worker hands the job back to the queue; a cancelled job stays cancelled.
    Everything fetched so far is already stored and logged."""
    refresh_status(session, job, lock=True)
    release_job(job)
    session.commit()
