"""Poll reports (`poll_reports`): what a completed poll changed, computed by the worker right
after it (domain/poll_report.py) and kept for 90 days – shown on the tracked search's page, as
a line in the list and in mails. A notable one also gets a few sentences by the AI
(services/ai_summaries.py) when that is switched on. The caller commits.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.deals import unusual_prices
from flighttracker.domain.locations import describe_locations
from flighttracker.domain.poll_report import (
    Flight,
    from_facts,
    notable,
    search_report,
    to_facts,
)
from flighttracker.i18n import DEFAULT_LOCALE
from flighttracker.models import FetchJob, JobStatus, PollReport, PriceHistory, Search, User
from flighttracker.providers.registry import FAKE_DATA_PROVIDERS
from flighttracker.services import ai, ai_summaries
from flighttracker.services.searches import spec_of

RETENTION = timedelta(days=90)

FlightKey = tuple[str, str, str, str, date, date | None]


def previous_poll_end(
    session: Session, search_id: int, job_id: int | None = None
) -> datetime | None:
    """When the completed poll before `job_id` (None: the latest completed one) ended."""
    statement = select(FetchJob.finished_at).where(
        FetchJob.search_id == search_id,
        FetchJob.trip_id.is_(None),
        FetchJob.status == JobStatus.DONE,
    )
    if job_id is not None:
        statement = statement.where(FetchJob.id != job_id)
    return session.scalar(statement.order_by(FetchJob.finished_at.desc(), FetchJob.id.desc()))


def _series(session: Session, search: Search, today: date) -> dict[FlightKey, list]:
    """Every real observation of the upcoming flights for the current passengers, oldest first."""
    filters = spec_of(search).filters
    rows = session.execute(
        select(
            PriceHistory.origin_iata,
            PriceHistory.destination_iata,
            PriceHistory.cabin_class,
            PriceHistory.currency,
            PriceHistory.departure_date,
            PriceHistory.return_date,
            PriceHistory.price,
            PriceHistory.observed_at,
        )
        .where(
            PriceHistory.search_id == search.id,
            PriceHistory.departure_date >= today,
            PriceHistory.adults == filters.adults,
            PriceHistory.children == filters.children,
            PriceHistory.provider.not_in(list(FAKE_DATA_PROVIDERS)),
        )
        .order_by(PriceHistory.observed_at, PriceHistory.id)
    )
    series: dict[FlightKey, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for r in rows:
        key = (r.origin_iata, r.destination_iata, str(r.cabin_class), r.currency, r.departure_date)
        series[(*key, r.return_date)].append((r.observed_at, r.price))
    return series


def observed_flights(
    session: Session,
    search: Search,
    today: date,
    *,
    current_since: datetime | None,
    compare_before: datetime | None,
) -> tuple[str, list[Flight]]:
    """Real, upcoming flights whose latest price was observed at or after `current_since`
    (None: any), with the last and the lowest price observed before `compare_before` – in the
    currency most of them have."""
    series = _series(session, search, today)
    latest = {
        key: values[-1]
        for key, values in series.items()
        if current_since is None or values[-1][0] >= current_since
    }
    if not latest:
        return "", []
    currency = Counter(key[3] for key in latest).most_common(1)[0][0]
    flights = []
    for key, (_, price) in latest.items():
        if key[3] != currency:
            continue
        before = [
            value
            for observed, value in series[key]
            if compare_before is not None and observed < compare_before
        ]
        origin, destination, cabin, _, departure, returning = key
        flights.append(
            Flight(
                origin,
                destination,
                cabin,
                departure,
                returning,
                price,
                previous=before[-1] if before else None,
                lowest_before=min(before) if before else None,
            )
        )
    return currency, sorted(flights, key=lambda f: (f.departure, f.price))


def _latest_row(session: Session, search_id: int, job_id: int | None = None) -> PollReport | None:
    statement = select(PollReport).where(PollReport.search_id == search_id)
    if job_id is not None:
        statement = statement.where(PollReport.job_id != job_id)
    return session.scalar(statement.order_by(PollReport.created_at.desc(), PollReport.id.desc()))


def record_search(session: Session, search: Search, job: FetchJob, now: datetime) -> PollReport:
    """The report of a completed poll of `search` (replaces one of the same job)."""
    since = previous_poll_end(session, search.id, job.id)
    currency, flights = observed_flights(
        session, search, now.date(), current_since=since, compare_before=since
    )
    previous = _latest_row(session, search.id, job.id)
    report = search_report(
        currency, flights, from_facts(previous.facts) if previous else None, unusual_prices(flights)
    )
    row = session.scalar(select(PollReport).where(PollReport.job_id == job.id))
    if row is None:
        row = PollReport(job_id=job.id, search_id=search.id, created_at=now)
        session.add(row)
    row.facts, row.notable = to_facts(report), notable(report)
    row.summary = row.summary_locale = None
    prune(session, now)
    return row


def prune(session: Session, now: datetime) -> None:
    session.execute(delete(PollReport).where(PollReport.created_at < now - RETENTION))
    session.flush()


def route_text(search: Search) -> str:
    spec = spec_of(search)
    origins = describe_locations(spec.origins, spec.country_airports)
    return f"{origins} → {describe_locations(spec.destinations, spec.country_airports)}"


def owner_locale(session: Session, owner_id: int | None) -> tuple[User | None, str]:
    owner = session.get(User, owner_id) if owner_id is not None else None
    return owner, (owner.locale if owner and owner.locale else DEFAULT_LOCALE)


def wants_summary(session: Session, settings: Settings, row: PollReport, now: datetime) -> bool:
    """Only notable polls, only with AI set up and switched on, and not right after a failed
    request (a server that does not answer must not slow down every poll)."""
    return (
        row.notable
        and ai.ai_enabled(settings)
        and settings.ai_poll_summaries
        and not ai_summaries.paused(session, now)
    )


def summarize_search(
    session: Session, settings: Settings, row: PollReport, search: Search, now: datetime
) -> bool:
    """The AI's words for a notable poll, in the owner's language; False when not asked or the
    request failed (it is in the usage log either way)."""
    report = from_facts(row.facts)
    if report is None or not wants_summary(session, settings, row, now):
        return False
    owner, locale = owner_locale(session, search.owner_id)
    usage = ai.UsageLog(session, "summary", owner.id if owner else None)
    try:
        text = ai_summaries.summarize_search(
            settings, search.name, route_text(search), report, locale, usage
        )
    except ai.AIError:
        return False
    row.summary, row.summary_locale = text, locale
    session.flush()
    return True


def latest_reports(session: Session, search_ids: list[int]) -> dict[int, PollReport]:
    """The latest report per tracked search (one query)."""
    if not search_ids:
        return {}
    rows = session.scalars(
        select(PollReport)
        .where(PollReport.search_id.in_(search_ids))
        .order_by(PollReport.search_id, PollReport.created_at.desc(), PollReport.id.desc())
        .distinct(PollReport.search_id)
    )
    return {row.search_id: row for row in rows}
