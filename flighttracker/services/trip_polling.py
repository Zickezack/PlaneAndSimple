"""Trip polls: which dates and queries a leg needs and how its quotes are stored.

The Trip counterpart of `ingestion.py`; used by the worker's Trip job.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.domain.trips import (
    LayoverRule,
    TripLegQuote,
    arrival_at,
    connection_fits,
)
from flighttracker.models import (
    FetchJob,
    PriceHistory,
    PriceSource,
    QueryLog,
    QueryOutcome,
    Search,
    SearchRevision,
    Trip,
    TripLeg,
)
from flighttracker.providers.base import PriceQuery


def month_groups(days: tuple[date, ...]) -> dict[date, tuple[date, ...]]:
    grouped: dict[date, list[date]] = {}
    for day in days:
        grouped.setdefault(day.replace(day=1), []).append(day)
    return {month: tuple(values) for month, values in grouped.items()}


def trip_query(
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


def next_leg_dates(
    trip: Trip,
    previous_paths: list[tuple[TripLegQuote, ...]],
    following_leg: TripLeg,
    today: date,
) -> tuple[date, ...]:
    """Possible departure calendar days based on arrival plus the layover window."""
    dates: set[date] = set()
    minimum_days = following_leg.min_layover_days or 0
    for path in previous_paths:
        arrival = arrival_at(path[-1])
        if arrival is None:
            continue
        earliest_day = arrival.date() + timedelta(days=minimum_days)
        latest_day = (
            arrival.date() + timedelta(days=following_leg.max_layover_days)
            if following_leg.max_layover_days is not None
            else trip.ends_on
        )
        latest_day = min(latest_day, trip.ends_on)
        if latest_day < earliest_day:
            continue
        day = max(earliest_day, today)
        while day <= latest_day:
            dates.add(day)
            day += timedelta(days=1)
    return tuple(sorted(dates))


def connect_quotes(
    previous_paths: list[tuple[TripLegQuote, ...]],
    quotes: list[TripLegQuote],
    rule: LayoverRule,
    *,
    final_leg: bool,
    ends_on: date,
) -> tuple[list[TripLegQuote], list[tuple[TripLegQuote, ...]]]:
    """Keep only quotes that extend a feasible path, returning quotes and extended paths."""
    connected: list[TripLegQuote] = []
    paths: list[tuple[TripLegQuote, ...]] = []
    for quote in quotes:
        for path in previous_paths:
            if not connection_fits(path[-1], quote, rule):
                continue
            if final_leg:
                arrival = arrival_at(quote)
                if arrival is None or arrival.date() > ends_on:
                    continue
            if quote.currency != path[0].currency:
                continue
            if quote not in connected:
                connected.append(quote)
            extended = (*path, quote)
            if extended not in paths:
                paths.append(extended)
            if len(paths) >= 1000:
                return connected, paths
    return connected, paths


def store_trip_quotes(
    session: Session,
    *,
    job: FetchJob,
    search: Search,
    revision: SearchRevision,
    cabin_class,
    departure_month: date,
    provider_name: str,
    origin: str,
    destination: str,
    quotes: list,
    adults: int,
    children: int,
    error: str | None,
    started_at: datetime,
    duration_ms: int,
) -> QueryLog:
    """Persist a leg query under the parent Trip job, keeping the ordinary log format."""
    stored = 0
    if quotes:
        rows = [
            {
                "search_id": search.id,
                "search_revision_id": revision.id,
                "source": PriceSource.LIVE,
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
        from sqlalchemy.dialects.postgresql import insert

        stored = len(
            session.execute(
                insert(PriceHistory)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_price_history_observation")
                .returning(PriceHistory.id)
            ).all()
        )
    entry = QueryLog(
        job_id=job.id,
        search_id=search.id,
        search_name=search.name,
        provider=provider_name,
        origin_iata=origin,
        destination_iata=destination,
        departure_month=departure_month,
        cabin_class=cabin_class,
        outcome=QueryOutcome.FAILED if error else QueryOutcome.OK if quotes else QueryOutcome.EMPTY,
        quotes_found=len(quotes),
        quotes_stored=stored,
        error=error,
        results=[
            {
                "date": quote.departure_date.isoformat(),
                "return": quote.return_date.isoformat() if quote.return_date else None,
                "price": str(quote.price),
                "currency": quote.currency,
                "stops": quote.stops,
                "details": quote.details,
            }
            for quote in quotes
        ],
        started_at=started_at,
        duration_ms=duration_ms,
    )
    session.add(entry)
    session.flush()
    return entry
