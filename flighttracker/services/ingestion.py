import logging
import time
from calendar import monthrange
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flighttracker.domain.spec import SearchSpec, departure_months, route_pairs
from flighttracker.models import (
    CalendarObservation,
    FetchJob,
    PriceHistory,
    PriceSource,
    QueryKind,
    QueryLog,
    QueryOutcome,
    SearchStatus,
)
from flighttracker.providers.base import (
    CalendarPrice,
    CalendarQuery,
    FlightPriceProvider,
    PriceQuery,
    PriceQuote,
    ProviderError,
)
from flighttracker.services.searches import current_revision_id, get_search, spec_of

log = logging.getLogger(__name__)


def build_queries(
    spec: SearchSpec, today: date, *, currency: str | None = None
) -> list[PriceQuery]:
    filters = spec.filters
    return [
        PriceQuery(
            origin=origin,
            destination=destination,
            departure_month=month,
            cabin_class=cabin,
            trip_type=filters.trip_type,
            max_stops=filters.max_stops,
            stay_days_min=filters.stay_days_min,
            stay_days_max=filters.stay_days_max,
            adults=filters.adults,
            children=filters.children,
            currency=currency or filters.currency,
            days_per_month=filters.days_per_month,
        )
        for origin, destination in route_pairs(spec)
        for cabin in sorted(filters.cabin_classes)
        for month in departure_months(filters.months_ahead, today)
    ]


def build_calendar_queries(
    spec: SearchSpec, today: date, *, currency: str | None = None
) -> list[CalendarQuery]:
    """Price calendar of every route, cabin and stay length, from tomorrow to the end of the
    last polled month (the provider splits it into requests)."""
    filters = spec.filters
    last_month = departure_months(filters.months_ahead, today)[-1]
    last_day = last_month.replace(day=monthrange(last_month.year, last_month.month)[1])
    return [
        CalendarQuery(
            origin=origin,
            destination=destination,
            cabin_class=cabin,
            first_day=today + timedelta(days=1),
            last_day=last_day,
            stay_days=stay,
            max_stops=filters.max_stops,
            adults=filters.adults,
            children=filters.children,
            currency=currency or filters.currency,
        )
        for origin, destination in route_pairs(spec)
        for cabin in sorted(filters.cabin_classes)
        for stay in filters.stay_lengths or [None]
    ]


def _accept(quote: PriceQuote, query: PriceQuery) -> bool:
    """Guard against providers that ignore parts of the query."""
    if query.departure_dates is not None and quote.departure_date not in query.departure_dates:
        return False
    if quote.currency != query.currency:
        log.warning(
            "Quote in %s instead of %s discarded (%s)",
            quote.currency,
            query.currency,
            quote.provider,
        )
        return False
    return query.max_stops is None or quote.stops is None or quote.stops <= query.max_stops


def store_quotes(
    session: Session,
    *,
    search_id: int,
    revision_id: int,
    source: PriceSource,
    quotes: Iterable[PriceQuote],
    adults: int,
    children: int,
) -> int:
    rows = [
        {
            "search_id": search_id,
            "search_revision_id": revision_id,
            "source": source,
            "provider": quote.provider,
            "origin_iata": quote.origin,
            "destination_iata": quote.destination,
            "departure_date": quote.departure_date,
            "return_date": quote.return_date,
            "cabin_class": quote.cabin_class,
            "stops": quote.stops,
            "price": quote.price,
            "currency": quote.currency,
            "adults": adults,
            "children": children,
            "airline": quote.airline,
            "details": quote.details,
            "observed_at": quote.observed_at,
        }
        for quote in quotes
    ]
    if not rows:
        return 0
    # RETURNING instead of rowcount: rowcount is unreliable for multi-row ON CONFLICT inserts.
    statement = (
        insert(PriceHistory)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_price_history_observation")
        .returning(PriceHistory.id)
    )
    return len(session.execute(statement).all())


def store_calendar(
    session: Session,
    *,
    search_id: int,
    provider: str,
    query: CalendarQuery,
    prices: Iterable[CalendarPrice],
    observed_at: datetime,
) -> int:
    rows = [
        {
            "search_id": search_id,
            "provider": provider,
            "origin_iata": query.origin,
            "destination_iata": query.destination,
            "cabin_class": query.cabin_class,
            "departure_date": price.departure_date,
            "return_date": price.return_date,
            "price": price.price,
            "currency": price.currency,
            "adults": query.adults,
            "children": query.children,
            "observed_at": observed_at,
        }
        for price in prices
    ]
    if not rows:
        return 0
    statement = (
        insert(CalendarObservation)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_price_calendar_observation")
        .returning(CalendarObservation.id)
    )
    return len(session.execute(statement).all())


@dataclass(frozen=True)
class JobPlan:
    """The provider queries of one poll job, resolved when the job starts."""

    job_id: int
    search_id: int
    search_name: str
    revision_id: int
    queries: list[PriceQuery]
    calendar_queries: list[CalendarQuery] = field(default_factory=list)


def _job_entries(session: Session, job_id: int) -> list[QueryLog]:
    """The job's fare queries – calendar requests neither resume nor count towards the job."""
    return list(
        session.scalars(
            select(QueryLog).where(QueryLog.job_id == job_id, QueryLog.kind == QueryKind.FARES)
        )
    )


def _query_key(origin: str, destination: str, month: date, cabin: str) -> tuple:
    return (origin, destination, month, cabin)


def plan_job(
    session: Session,
    job: FetchJob,
    now: datetime,
    *,
    currency: str | None = None,
    calendar: bool = False,
) -> JobPlan | None:
    """None when there is nothing to do (Suchabo deleted, archived or paused meanwhile).

    A resumed job (worker restart, retry) skips the queries it already completed; only
    missing and failed ones run again.
    """
    search = get_search(session, job.search_id)
    if search is None or search.is_archived or search.status is not SearchStatus.ACTIVE:
        return None
    done = {
        _query_key(e.origin_iata, e.destination_iata, e.departure_month, e.cabin_class)
        for e in _job_entries(session, job.id)
        if e.outcome is not QueryOutcome.FAILED
    }
    queries = [
        q
        for q in build_queries(spec_of(search), now.date(), currency=currency)
        if _query_key(q.origin, q.destination, q.departure_month, q.cabin_class) not in done
    ]
    spec = spec_of(search)
    return JobPlan(
        job_id=job.id,
        search_id=search.id,
        search_name=search.name,
        revision_id=current_revision_id(session, search),
        queries=queries,
        calendar_queries=build_calendar_queries(spec, now.date(), currency=currency)
        if calendar
        else [],
    )


def run_query(
    session: Session,
    plan: JobPlan,
    query: PriceQuery,
    provider: FlightPriceProvider,
    *,
    now: datetime,
    monotonic: Callable[[], float] = time.monotonic,
) -> QueryLog:
    """Run one provider query, store its prices and write the query log entry.

    A `ProviderError` only fails this query (logged); other exceptions propagate.
    """
    started = monotonic()
    entry = QueryLog(
        job_id=plan.job_id,
        search_id=plan.search_id,
        search_name=plan.search_name,
        provider=provider.name,
        origin_iata=query.origin,
        destination_iata=query.destination,
        departure_month=query.departure_month,
        cabin_class=query.cabin_class,
        started_at=now,
        quotes_found=0,
        quotes_stored=0,
    )
    try:
        quotes = [q for q in provider.fetch_current(query) if _accept(q, query)]
    except ProviderError as exc:
        entry.outcome = QueryOutcome.FAILED
        entry.error = str(exc)
    else:
        entry.quotes_found = len(quotes)
        entry.results = [
            {
                "date": q.departure_date.isoformat(),
                "return": q.return_date.isoformat() if q.return_date else None,
                "price": str(q.price),
                "currency": q.currency,
                "stops": q.stops,
            }
            for q in sorted(
                quotes, key=lambda q: (q.departure_date, q.return_date or q.departure_date)
            )
        ]
        entry.quotes_stored = store_quotes(
            session,
            search_id=plan.search_id,
            revision_id=plan.revision_id,
            source=PriceSource.LIVE,
            quotes=quotes,
            adults=query.adults,
            children=query.children,
        )
        entry.outcome = QueryOutcome.OK if quotes else QueryOutcome.EMPTY
    entry.duration_ms = int((monotonic() - started) * 1000)
    session.add(entry)
    session.flush()
    return entry


def run_calendar_query(
    session: Session,
    plan: JobPlan,
    query: CalendarQuery,
    provider: FlightPriceProvider,
    *,
    now: datetime,
    monotonic: Callable[[], float] = time.monotonic,
) -> QueryLog:
    """One price-calendar query (heat map); a `ProviderError` only fails this entry."""
    started = monotonic()
    entry = QueryLog(
        job_id=plan.job_id,
        search_id=plan.search_id,
        search_name=plan.search_name,
        provider=provider.name,
        kind=QueryKind.CALENDAR,
        origin_iata=query.origin,
        destination_iata=query.destination,
        departure_month=query.first_day.replace(day=1),
        cabin_class=query.cabin_class,
        started_at=now,
        quotes_found=0,
        quotes_stored=0,
    )
    try:
        prices = [p for p in provider.fetch_calendar(query) if p.currency == query.currency]
    except ProviderError as exc:
        entry.outcome = QueryOutcome.FAILED
        entry.error = str(exc)
    else:
        entry.quotes_found = len(prices)
        entry.quotes_stored = store_calendar(
            session,
            search_id=plan.search_id,
            provider=provider.name,
            query=query,
            prices=prices,
            observed_at=now,
        )
        entry.outcome = QueryOutcome.OK if prices else QueryOutcome.EMPTY
    entry.duration_ms = int((monotonic() - started) * 1000)
    session.add(entry)
    session.flush()
    return entry


def finish_job(
    session: Session, job: FetchJob, entries: list[QueryLog], now: datetime
) -> str | None:
    """Record the counters on the job and the poll time on the Suchabo.

    `entries` are this run's log entries; results of earlier (interrupted) runs of the same
    job count too, a query retried successfully replaces its earlier failure.
    Returns the last error message if any query failed (all failed → the job is retried).
    """
    latest: dict[tuple, QueryLog] = {}
    fares = [e for e in entries if e.kind is QueryKind.FARES]
    for entry in sorted({*_job_entries(session, job.id), *fares}, key=lambda e: e.id):
        key = _query_key(
            entry.origin_iata, entry.destination_iata, entry.departure_month, entry.cabin_class
        )
        latest[key] = entry
    results = list(latest.values())
    failed = [e for e in results if e.outcome is QueryOutcome.FAILED]
    job.queries_total = len(results)
    job.queries_failed = len(failed)
    job.quotes_stored = sum(e.quotes_stored for e in results)
    search = get_search(session, job.search_id)
    if search is not None and len(failed) < len(results):
        search.last_polled_at = now
    return failed[-1].error if failed else None


def all_queries_failed(job: FetchJob) -> bool:
    return job.queries_total > 0 and job.queries_failed == job.queries_total


def run_fetch_job(
    session: Session, job: FetchJob, provider: FlightPriceProvider, *, now: datetime
) -> int:
    """All queries of a job in one transaction (tests, scripts). The worker commits per query.

    Returns the number of new price rows.
    """
    plan = plan_job(session, job, now, calendar=provider.calendar_days_per_request is not None)
    if plan is None:
        return 0
    entries = [run_query(session, plan, query, provider, now=now) for query in plan.queries]
    for query in plan.calendar_queries:
        run_calendar_query(session, plan, query, provider, now=now)
    error = finish_job(session, job, entries, now)
    if all_queries_failed(job):
        raise ProviderError(error or "All queries failed")
    return job.quotes_stored
