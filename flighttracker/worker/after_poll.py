"""What the worker does after a completed poll of a tracked search: report what changed, let
the AI word a notable report, send the price alert mails.

Every step runs on its own and commits: a failing AI provider or mail server never fails the
poll and never stops the next step.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import FetchJob, PollReport, Search
from flighttracker.services import poll_reports, price_alerts, report_texts

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
    report = step(
        session,
        f"Poll report of job {job_id}",
        lambda: poll_reports.record_search(session, search, job, now),
    )
    report_id = report.id if report is not None else None
    if report_id is not None and step(
        session,
        f"AI summary of job {job_id}",
        lambda: poll_reports.summarize_search(
            session, settings, session.get(PollReport, report_id), search, now
        ),
    ):
        log.info("Poll of search %d summarised by the AI", search.id)

    def blocks(locale: str) -> list[str]:
        row = session.get(PollReport, report_id) if report_id is not None else None
        return report_texts.mail_blocks(row, locale)

    sent = step(
        session,
        f"Price alerts of search {search.id}",
        lambda: price_alerts.notify(session, settings, search, now, extra=blocks),
    )
    if sent:
        log.info("%d price alert mails sent for search %d", sent, search.id)
