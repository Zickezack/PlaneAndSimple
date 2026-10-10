"""The coordinated poll of a Trip (all legs in one job)."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from flighttracker.domain.filters import SearchFilters
from flighttracker.domain.spec import route_pairs
from flighttracker.domain.trips import LayoverRule, TripLegQuote, arrival_at, departure_at
from flighttracker.models import FetchJob, SearchRevision, SearchStatus
from flighttracker.providers.base import FlightPriceProvider, ProviderError
from flighttracker.services.ingestion import (
    accept_quote,
)
from flighttracker.services.jobs import (
    mark_done,
    mark_failed,
    refresh_status,
)
from flighttracker.services.searches import current_revision_id, spec_of
from flighttracker.services.trip_polling import (
    connect_quotes,
    month_groups,
    next_leg_dates,
    store_trip_quotes,
    trip_query,
)
from flighttracker.services.trips import get_trip
from flighttracker.worker.job_control import interrupt, should_end

log = logging.getLogger("flighttracker.worker")

Clock = Callable[[], datetime]


def process_trip_job(
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
            if should_end(session, job, should_stop):
                interrupt(session, job)
                return
            if position == 0:
                departure_days = first_dates
            else:
                departure_days = next_leg_dates(trip, paths, leg, now.date())
            following_paths: list[tuple[TripLegQuote, ...]] = []
            for origin, destination in route_pairs(spec_of(leg.search)):
                for month, days in month_groups(departure_days).items():
                    if should_end(session, job, should_stop):
                        interrupt(session, job)
                        return
                    query = trip_query(
                        leg.search, cabin, month, days, origin, destination, currency
                    )
                    started_at = clock()
                    monotonic_started = time.monotonic()
                    error = None
                    try:
                        quotes = [
                            quote
                            for quote in provider.fetch_current(query)
                            if accept_quote(quote, query)
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
