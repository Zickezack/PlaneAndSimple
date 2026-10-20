"""What the worker does after a completed poll: report what changed and let the AI word a
notable report; for a tracked search also mail unusually cheap prices and the price alerts.

Every step runs on its own and commits: a failing AI provider or mail server never fails the
poll and never stops the next step.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import FetchJob, PollReport, Search, Trip
from flighttracker.services import (
    deal_radar,
    poll_reports,
    price_alerts,
    report_texts,
    trip_reports,
)

log = logging.getLogger("flighttracker.worker")


def step[T](session: Session, name: str, action: Callable[[], T]) -> T | None:
    try:
        result = action()
        session.commit()
    except Exception:
        session.rollback()
        log.exception("%s failed", name)
        return None
    return result


def after_search_poll(session: Session, settings: Settings, job_id: int, now: datetime) -> None:
    job = session.get(FetchJob, job_id)
    search = session.get(Search, job.search_id) if job else None
    if job is None or search is None:
        return
    recorded = step(
        session,
        f"Poll report of job {job_id}",
        lambda: poll_reports.record_search(session, search, job, now),
    )
    report_id = recorded.id if recorded is not None else None

    def report() -> PollReport | None:
        # Read again after every step: a failed one rolls the session back.
        return session.get(PollReport, report_id) if report_id is not None else None

    if report_id is not None and step(
        session,
        f"AI summary of job {job_id}",
        lambda: poll_reports.summarize_search(session, settings, report(), search, now),
    ):
        log.info("Poll of search %d summarised by the AI", search.id)
    deals = step(
        session,
        f"Deal mails of search {search.id}",
        lambda: deal_radar.notify(session, settings, search, report(), now),
    )
    if deals:
        log.info("%d mails about unusually cheap prices sent for search %d", deals, search.id)
    sent = step(
        session,
        f"Price alerts of search {search.id}",
        lambda: price_alerts.notify(
            session,
            settings,
            search,
            now,
            extra=lambda locale: report_texts.mail_blocks(report(), locale),
        ),
    )
    if sent:
        log.info("%d price alert mails sent for search %d", sent, search.id)


def after_trip_poll(session: Session, settings: Settings, job_id: int, now: datetime) -> None:
    job = session.get(FetchJob, job_id)
    trip = session.get(Trip, job.trip_id) if job and job.trip_id else None
    if job is None or trip is None:
        return
    recorded = step(
        session,
        f"Trip report of job {job_id}",
        lambda: trip_reports.record_trip(session, trip, job, now),
    )
    if recorded is None:
        return
    report_id = recorded.id
    if step(
        session,
        f"AI summary of job {job_id}",
        lambda: trip_reports.summarize_trip(
            session, settings, session.get(PollReport, report_id), trip, now
        ),
    ):
        log.info("Poll of Trip %d summarised by the AI", trip.id)
