"""Trip reports (`poll_reports` with a `trip_id`): what a completed Trip poll changed – its
working options and the cheapest one (domain/trip_report.py) – and the AI's words for a notable
one. The caller commits.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.trip_report import LegPrice, from_facts, notable, to_facts, trip_report
from flighttracker.models import FetchJob, PollReport, Trip
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import ai, ai_summaries, poll_reports
from flighttracker.services.trips import itineraries_for_trip


def _latest_row(session: Session, trip_id: int, job_id: int | None = None) -> PollReport | None:
    statement = select(PollReport).where(PollReport.trip_id == trip_id)
    if job_id is not None:
        statement = statement.where(PollReport.job_id != job_id)
    return session.scalar(statement.order_by(PollReport.created_at.desc(), PollReport.id.desc()))


def record_trip(session: Session, trip: Trip, job: FetchJob, now: datetime) -> PollReport:
    """The report of a completed poll of `trip` (replaces one of the same job)."""
    options = [
        option
        for option in itineraries_for_trip(session, trip, FAKE_DATA_PROVIDERS, today=now.date())
        if not option.fake
    ]
    previous_row = _latest_row(session, trip.id, job.id)
    previous = from_facts(previous_row.facts) if previous_row else None
    if options:
        cheapest = options[0]  # cheapest first
        currency = cheapest.currency
        legs = [
            LegPrice(
                leg.origin,
                leg.destination,
                leg.departure_date,
                (getattr(leg, "details", None) or {}).get("departure_time"),
                leg.price,
                getattr(leg, "previous_price", None),
            )
            for leg in cheapest.legs
        ]
        count = sum(1 for option in options if option.currency == currency)
    else:
        currency, legs, count = (previous.currency if previous else ""), [], 0
    report = trip_report(currency, count, legs, previous)
    row = session.scalar(select(PollReport).where(PollReport.job_id == job.id))
    if row is None:
        row = PollReport(job_id=job.id, trip_id=trip.id, created_at=now)
        session.add(row)
    row.facts, row.notable = to_facts(report), notable(report)
    row.summary = row.summary_locale = None
    poll_reports.prune(session, now)
    return row


def summarize_trip(
    session: Session, settings: Settings, row: PollReport, trip: Trip, now: datetime
) -> bool:
    """The AI's words for a notable Trip poll, in the owner's language (see
    `poll_reports.summarize_search`)."""
    report = from_facts(row.facts)
    if report is None or not poll_reports.wants_summary(session, settings, row, now):
        return False
    owner, locale = poll_reports.owner_locale(session, trip.owner_id)
    usage = ai.UsageLog(session, "trip_summary", owner.id if owner else None)
    try:
        text = ai_summaries.summarize_trip(settings, trip.name, report, locale, usage)
    except ai.AIError:
        return False
    row.summary, row.summary_locale = text, locale
    session.flush()
    return True


def latest_trip_reports(session: Session, trip_ids: list[int]) -> dict[int, PollReport]:
    """The latest report per Trip (one query)."""
    if not trip_ids:
        return {}
    rows = session.scalars(
        select(PollReport)
        .where(PollReport.trip_id.in_(trip_ids))
        .order_by(PollReport.trip_id, PollReport.created_at.desc(), PollReport.id.desc())
        .distinct(PollReport.trip_id)
    )
    return {row.trip_id: row for row in rows}
