"""Google Flights adapter based on the open-source scraper `fast-flights` (extra `scraper`).

Live fares, no history. Scraping may break whenever Google changes its page and is a
Terms-of-Service grey area – keep request rates low and treat this adapter as best effort.

fast-flights builds the query URL and provides the HTTP client; the result page is read by
our own `parse_fares`. From European IP addresses Google first shows a cookie consent page
instead of the results, so requests carry a consent cookie (rejecting all optional cookies).
"""

import calendar
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from flighttracker.domain.filters import CabinClass
from flighttracker.providers.base import (
    FlightPriceProvider,
    PriceQuery,
    PriceQuote,
    ProviderError,
    return_dates,
)

log = logging.getLogger(__name__)

URL = "https://www.google.com/travel/flights"
# "Reject all" consent – without it, EU/CH requests only get the "Before you continue" page.
CONSENT_COOKIE = "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiAo_CmBg"

_SEAT = {
    CabinClass.ECONOMY: "economy",
    CabinClass.PREMIUM_ECONOMY: "premium-economy",
    CabinClass.BUSINESS: "business",
    CabinClass.FIRST: "first",
}


@dataclass(frozen=True)
class FareOption:
    price: int
    stops: int
    airline: str | None
    details: dict | None = None


@dataclass(frozen=True)
class FareRequest:
    origin: str
    destination: str
    departure: date
    return_date: date | None
    seat: str
    adults: int
    currency: str
    max_stops: int | None
    children: int = 0


SearchFn = Callable[[FareRequest], list[FareOption]]

_RESULTS_SCRIPT = re.compile(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', re.S)


def _time(value: list | None) -> str | None:
    """Google omits zero components: [8] = 08:00, [None, 31] = 00:31, None = unknown."""
    if not value:
        return None
    hours, minutes = ([*value, None, None])[:2]
    return f"{hours or 0:02d}:{minutes or 0:02d}"


def _date(value: list | None) -> str | None:
    try:
        return f"{value[0]:04d}-{value[1]:02d}-{value[2]:02d}"
    except (IndexError, TypeError):
        return None


def _at(index: int, values: list):
    return values[index] if len(values) > index else None


def _segment(segment: list) -> dict:
    number = _at(22, segment) or []
    flight = f"{number[0]} {number[1]}" if len(number) > 1 and number[0] and number[1] else None
    return {
        "flight": flight,
        "from": _at(3, segment),
        "to": _at(6, segment),
        "departure_date": _date(_at(20, segment)),
        "departure_time": _time(_at(8, segment)),
        "arrival_date": _date(_at(21, segment)),
        "arrival_time": _time(_at(10, segment)),
        "duration_min": _at(11, segment),
        "aircraft": _at(17, segment),
    }


def _details(itinerary: list) -> dict | None:
    """Outbound itinerary as shown by Google; best effort – missing fields stay None."""
    try:
        segments = [_segment(s) for s in itinerary[2] or []]
        return {
            "airlines": list(itinerary[1] or []),
            "departure_time": _time(_at(5, itinerary)),
            "arrival_date": _date(_at(7, itinerary)),
            "arrival_time": _time(_at(8, itinerary)),
            "duration_min": _at(9, itinerary),
            "segments": segments,
        }
    except (IndexError, TypeError):
        return None


def _price(entry: list) -> int | None:
    try:
        return int(entry[1][0][1])
    except (IndexError, TypeError, ValueError):
        return None  # Google lists some itineraries without a price


def parse_fares(html: str) -> list[FareOption]:
    """Price, stops and airline of every priced itinerary on a Google Flights result page.

    Our own reading of the page data instead of fast-flights' parser, which only reads the
    "other flights" list (missing the "best flights", often the only direct ones) and crashes
    on empty result lists or itineraries without a price. Raises ValueError if the page does
    not have the expected structure.
    """
    match = _RESULTS_SCRIPT.search(html)
    if match is None:
        raise ValueError("result data missing")
    data = match.group(1).split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []  # Google's "no flights found"
    payload = json.loads(data)
    options = []
    # payload[2]: "best flights", payload[3]: "other flights"; each [entries, ...] or None.
    for group in (payload[2], payload[3]):
        for entry in (group[0] or []) if group else []:
            price = _price(entry)
            if price is None:
                continue
            itinerary = entry[0]
            airlines = itinerary[1] or []
            options.append(
                FareOption(
                    price=price,
                    stops=max(len(itinerary[2]) - 1, 0),
                    airline=airlines[0] if airlines else None,
                    details=_details(itinerary),
                )
            )
    return options


def fast_flights_search(request: FareRequest) -> list[FareOption]:
    """The only place that touches the `fast-flights` API (pinned in pyproject.toml)."""
    try:
        from fast_flights import FlightQuery, Passengers, create_query
        from primp import Client  # HTTP client of fast-flights (browser impersonation)
    except ImportError as exc:
        raise ProviderError("Package fast-flights missing: pip install '.[scraper]'") from exc

    legs = [
        FlightQuery(
            date=request.departure.isoformat(),
            from_airport=request.origin,
            to_airport=request.destination,
        )
    ]
    if request.return_date is not None:
        legs.append(
            FlightQuery(
                date=request.return_date.isoformat(),
                from_airport=request.destination,
                to_airport=request.origin,
            )
        )
    query = create_query(
        flights=legs,
        seat=request.seat,
        trip="round-trip" if request.return_date is not None else "one-way",
        passengers=Passengers(adults=request.adults, children=request.children),
        currency=request.currency,
        max_stops=request.max_stops,
    )
    try:
        client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True)
        html = client.get(URL, params=query.params(), headers={"Cookie": CONSENT_COOKIE}).text
        if "<title>Before you continue" in html:
            raise ProviderError("Google Flights shows its cookie consent page instead of results")
        options = parse_fares(html)
        # Link to exactly this search on Google Flights (for checking and booking).
        url = f"{URL}?{urlencode(query.params())}"
        return [replace(o, details={**o.details, "url": url}) if o.details else o for o in options]
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Google Flights query failed ({type(exc).__name__})") from None


def sample_days(month: date, count: int, after: date | None = None) -> list[date]:
    """`count` evenly spread departure days within the month, all later than `after`.

    In the current month only the remaining days are sampled, so no request is wasted.
    """
    days_in_month = calendar.monthrange(month.year, month.month)[1]
    first = 1
    if after is not None and (after.year, after.month) == (month.year, month.month):
        first = after.day + 1
    elif after is not None and after >= month.replace(day=days_in_month):
        return []
    available = days_in_month - first + 1
    if available <= 0:
        return []
    step = available / min(count, available)
    return sorted({month.replace(day=first + int(i * step)) for i in range(min(count, available))})


class GoogleFlightsProvider(FlightPriceProvider):
    """Prices are totals for all passengers (adults and children)."""

    name = "google_flights"
    supports_children = True
    samples_days = True
    supports_connection_times = True

    def __init__(
        self,
        *,
        request_delay_seconds: float = 3.0,
        search: SearchFn = fast_flights_search,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._delay = request_delay_seconds
        self._search = search
        self._clock = clock
        self._sleep = sleep

    def fetch_current(self, query: PriceQuery) -> list[PriceQuote]:
        """Cheapest fare per sampled day and stay length (one request each).

        Single failed requests are skipped; the query only fails if all of them do.
        """
        now = self._clock()
        quotes = []
        days = (
            [day for day in query.departure_dates if day > now.date()]
            if query.departure_dates is not None
            else sample_days(query.departure_month, query.days_per_month, now.date())
        )
        requests = [(day, ret) for day in days for ret in return_dates(query, day)]
        errors: list[ProviderError] = []
        for departure, return_date in requests:
            try:
                options = self._search(
                    FareRequest(
                        origin=query.origin,
                        destination=query.destination,
                        departure=departure,
                        return_date=return_date,
                        seat=_SEAT[query.cabin_class],
                        adults=query.adults,
                        currency=query.currency,
                        max_stops=query.max_stops,
                        children=query.children,
                    )
                )
            except ProviderError as exc:
                log.warning(
                    "%s→%s on %s skipped: %s", query.origin, query.destination, departure, exc
                )
                errors.append(exc)
                continue
            finally:
                self._sleep(self._delay)
            allowed = [o for o in options if query.max_stops is None or o.stops <= query.max_stops]
            if not allowed:
                continue
            cheapest = min(allowed, key=lambda o: o.price)
            quotes.append(
                PriceQuote(
                    provider=self.name,
                    origin=query.origin,
                    destination=query.destination,
                    departure_date=departure,
                    return_date=return_date,
                    cabin_class=query.cabin_class,
                    stops=cheapest.stops,
                    price=Decimal(cheapest.price),
                    currency=query.currency,
                    observed_at=now,
                    airline=cheapest.airline,
                    details=cheapest.details,
                )
            )
        if requests and len(errors) == len(requests):
            raise errors[-1]
        return quotes
