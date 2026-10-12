"""Price calendar: the request behind Google Flights' "Dates" view – the cheapest price of every
departure day of up to 61 days in one request. The format is not documented; the codes below
follow what the web app sends (as do open-source clients such as "fli")."""

import json
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

from flighttracker.domain.filters import CabinClass
from flighttracker.providers.base import CalendarPrice, CalendarQuery
from flighttracker.providers.google_flights.common import (
    ONE_WAY_CODE,
    ROUND_TRIP_CODE,
    frontend_post,
    stops_code,
    wrb_payloads,
)

CALENDAR_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGraph"
)
CALENDAR_DAYS_PER_REQUEST = 61
_CALENDAR_SEAT = {
    CabinClass.ECONOMY: 1,
    CabinClass.PREMIUM_ECONOMY: 2,
    CabinClass.BUSINESS: 3,
    CabinClass.FIRST: 4,
}

CalendarSearchFn = Callable[[str, str], str]


def calendar_request_body(query: CalendarQuery, first_day: date, last_day: date) -> str:
    """Form body (`f.req=…`) asking for every departure day from `first_day` to `last_day`."""

    def segment(origin: str, destination: str, day: date) -> list:
        stops = stops_code(query.max_stops)
        return (
            [[[[origin, 0]]], [[[destination, 0]]], None, stops, None, None, day.isoformat()]
            + [None] * 7
            + [3]
        )

    round_trip = query.stay_days is not None
    segments = [segment(query.origin, query.destination, first_day)]
    if round_trip:
        back = first_day + timedelta(days=query.stay_days)
        segments.append(segment(query.destination, query.origin, back))
    settings = [None, None, ROUND_TRIP_CODE if round_trip else ONE_WAY_CODE, None, []]
    settings += [_CALENDAR_SEAT[query.cabin_class], [query.adults, query.children, 0, 0]]
    settings += [None] * 6 + [segments, None, None, None, 1]
    filters = [None, settings, [first_day.isoformat(), last_day.isoformat()]]
    if round_trip:
        filters += [None, [query.stay_days, query.stay_days]]
    inner = json.dumps(filters, separators=(",", ":"))
    return "f.req=" + quote(json.dumps([None, inner], separators=(",", ":")))


def parse_calendar(text: str, currency: str) -> list[CalendarPrice]:
    """Cheapest price per departure day of a GetCalendarGraph response.

    Raises ValueError if the response has no calendar data at all; days without a price are
    skipped. Prices are totals for all passengers in the requested currency.
    """
    payloads = wrb_payloads(text)
    if not payloads:
        raise ValueError("calendar data missing")
    prices = []
    for payload in payloads:
        days = payload[-1] if isinstance(payload, list) and payload else None
        for day in days if isinstance(days, list) else []:
            try:
                price = day[2][0][1]
                departure = date.fromisoformat(day[0])
                returning = date.fromisoformat(day[1]) if day[1] else None
            except (IndexError, TypeError, ValueError):
                continue
            if price is not None:
                prices.append(CalendarPrice(departure, returning, Decimal(price), currency))
    return prices


def google_calendar_search(body: str, currency: str) -> str:
    """The only place that sends calendar requests."""
    return frontend_post(CALENDAR_URL, body, currency, "calendar query")
