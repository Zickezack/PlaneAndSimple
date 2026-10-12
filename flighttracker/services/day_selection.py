"""Calendar-driven departure days: which days a poll asks the provider for.

A provider that queries single days and has a price calendar (Google Flights) first reads the
calendar; each fare query then asks for half evenly spread days and half the cheapest days of
the calendar (`domain.sampling.choose_days`) instead of only evenly spread ones – the same
number of requests, but the chart shows the days that are really cheap.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from flighttracker.domain.filters import stay_lengths
from flighttracker.domain.sampling import choose_days
from flighttracker.providers.base import FlightPriceProvider, PriceQuery
from flighttracker.services.history import calendar_days

CalendarKey = tuple[str, str, str, str]  # origin, destination, cabin, currency


def uses_calendar_days(provider: FlightPriceProvider) -> bool:
    return provider.samples_days and provider.calendar_days_per_request is not None


def _key(query: PriceQuery) -> CalendarKey:
    return query.origin, query.destination, str(query.cabin_class), query.currency


def cheapest_calendar_prices(
    session: Session, search_id: int, query: PriceQuery, today: date
) -> dict[CalendarKey, dict[date, Decimal]]:
    """Latest calendar price per route, cabin and departure day: the cheapest over the stay
    lengths the Suchabo asks for now (calendar days of earlier stay lengths are ignored)."""
    stays = set(stay_lengths(query.trip_type, query.stay_days_min, query.stay_days_max)) or {None}
    prices: dict[CalendarKey, dict[date, Decimal]] = {}
    for day in calendar_days(
        session,
        search_id,
        today + timedelta(days=1),
        passengers=(query.adults, query.children),
    ):
        if day.stay_days not in stays:
            continue
        key = (day.origin, day.destination, day.cabin_class, day.currency)
        by_day = prices.setdefault(key, {})
        if day.departure_date not in by_day or day.price < by_day[day.departure_date]:
            by_day[day.departure_date] = day.price
    return prices


def with_calendar_days(
    session: Session, search_id: int, queries: list[PriceQuery], today: date
) -> list[PriceQuery]:
    """The fare queries of a poll with their departure days chosen from the price calendar."""
    if not queries:
        return queries
    prices = cheapest_calendar_prices(session, search_id, queries[0], today)
    return [
        replace(
            query,
            departure_dates=tuple(
                choose_days(
                    query.departure_month,
                    query.days_per_month,
                    today,
                    prices.get(_key(query), {}),
                )
            ),
        )
        for query in queries
    ]
