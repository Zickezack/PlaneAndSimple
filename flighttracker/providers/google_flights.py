"""Google Flights adapter based on the open-source scraper `fast-flights` (extra `scraper`).

Live fares, no history. Scraping may break whenever Google changes its page and is a
Terms-of-Service grey area – keep request rates low and treat this adapter as best effort.

Fares come from the request behind Google's result list ("GetShoppingResults"), sorted by
price with every result – the result page alone only holds the first ~10 "best" flights and
often misses the cheapest. If that request fails, the result page is read instead
(`parse_fares`). fast-flights builds the page URL and provides the HTTP client. From European
IP addresses Google first shows a cookie consent page instead of the results, so requests
carry a consent cookie (rejecting all optional cookies).
"""

import calendar
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote, urlencode

from flighttracker.domain.filters import CabinClass
from flighttracker.providers.base import (
    CalendarPrice,
    CalendarQuery,
    FlightPriceProvider,
    PriceQuery,
    PriceQuote,
    ProviderError,
    calendar_chunks,
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
# A flight of a chosen outbound itinerary: from, date, to, airline code, flight number.
SelectedLeg = tuple[str, str, str, str, str]
ReturnSearchFn = Callable[[FareRequest, list[SelectedLeg]], list[FareOption]]

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
    return _fare_options(json.loads(data))


def _fare_options(payload: list) -> list[FareOption]:
    """Priced itineraries of the result page data or a GetShoppingResults response."""
    options = []
    # payload[2]: "best flights", payload[3]: "other flights"; each [entries, ...] or None.
    for group in (_at(2, payload), _at(3, payload)):
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


def _client():
    try:
        from primp import Client  # HTTP client of fast-flights (browser impersonation)
    except ImportError as exc:
        raise ProviderError("Package fast-flights missing: pip install '.[scraper]'") from exc
    return Client(impersonate="chrome_145", impersonate_os="macos", referer=True)


def _page_query(request: FareRequest):
    """The only place that touches the `fast-flights` API (pinned in pyproject.toml)."""
    try:
        from fast_flights import FlightQuery, Passengers, create_query
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
    return create_query(
        flights=legs,
        seat=request.seat,
        trip="round-trip" if request.return_date is not None else "one-way",
        passengers=Passengers(adults=request.adults, children=request.children),
        currency=request.currency,
        max_stops=request.max_stops,
    )


def _with_url(options: list[FareOption], request: FareRequest) -> list[FareOption]:
    """Link to exactly this search on Google Flights (for checking and booking)."""
    url = f"{URL}?{urlencode(_page_query(request).params())}"
    return [replace(o, details={**o.details, "url": url}) if o.details else o for o in options]


def fast_flights_search(request: FareRequest) -> list[FareOption]:
    """Fares from the result page (only the flights it shows without "more flights")."""
    query = _page_query(request)
    try:
        html = _client().get(URL, params=query.params(), headers={"Cookie": CONSENT_COOKIE}).text
        if "<title>Before you continue" in html:
            raise ProviderError("Google Flights shows its cookie consent page instead of results")
        return _with_url(parse_fares(html), request)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Google Flights query failed ({type(exc).__name__})") from None


# Price calendar: the request behind Google Flights' "Dates" view – the cheapest price of every
# departure day of up to 61 days in one request. The format is not documented; the codes below
# follow what the web app sends (as do open-source clients such as "fli").
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
_CALENDAR_ROUND_TRIP, _CALENDAR_ONE_WAY = 1, 2

CalendarSearchFn = Callable[[str, str], str]


def _calendar_stops(max_stops: int | None) -> int:
    """Google's stop filter: 0 any, 1 non-stop, 2 at most one stop, 3 at most two stops."""
    if max_stops is None or max_stops >= 3:
        return 0
    return max_stops + 1


def calendar_request_body(query: CalendarQuery, first_day: date, last_day: date) -> str:
    """Form body (`f.req=…`) asking for every departure day from `first_day` to `last_day`."""

    def segment(origin: str, destination: str, day: date) -> list:
        stops = _calendar_stops(query.max_stops)
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
    settings = [None, None, _CALENDAR_ROUND_TRIP if round_trip else _CALENDAR_ONE_WAY, None, []]
    settings += [_CALENDAR_SEAT[query.cabin_class], [query.adults, query.children, 0, 0]]
    settings += [None] * 6 + [segments, None, None, None, 1]
    filters = [None, settings, [first_day.isoformat(), last_day.isoformat()]]
    if round_trip:
        filters += [None, [query.stay_days, query.stay_days]]
    inner = json.dumps(filters, separators=(",", ":"))
    return "f.req=" + quote(json.dumps([None, inner], separators=(",", ":")))


def _wrb_payloads(text: str) -> list:
    """Inner JSON of every "wrb.fr" chunk of a FlightsFrontendService response.

    The body starts with ")]}'"; the chunks follow either as plain JSON or each preceded by a
    line with its length in bytes (counting the newlines around the chunk).
    """
    raw = text.encode().lstrip().removeprefix(b")]}'").lstrip()
    chunks = []
    if raw[:1].isdigit():
        cursor = 0
        while cursor < len(raw):
            end = raw.find(b"\n", cursor)
            if end == -1:
                break
            length = max(int(raw[cursor:end]) - 1, 0)
            chunks.append(raw[end + 1 : end + 1 + length])
            cursor = end + 1 + length
    elif raw:
        chunks.append(raw)
    payloads = []
    for chunk in chunks:
        for row in json.loads(chunk):
            if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr":
                if isinstance(row[2], str):
                    payloads.append(json.loads(row[2]))
    return payloads


def parse_calendar(text: str, currency: str) -> list[CalendarPrice]:
    """Cheapest price per departure day of a GetCalendarGraph response.

    Raises ValueError if the response has no calendar data at all; days without a price are
    skipped. Prices are totals for all passengers in the requested currency.
    """
    payloads = _wrb_payloads(text)
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


def _frontend_post(url: str, body: str, currency: str, what: str) -> str:
    """POST to a FlightsFrontendService endpoint (same client and consent cookie as the page)."""
    client = _client()
    try:
        response = client.post(
            f"{url}?{urlencode({'curr': currency})}",
            content=body.encode(),
            headers={
                "Cookie": CONSENT_COOKIE,
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        text = response.text
    except Exception as exc:
        raise ProviderError(f"Google Flights {what} failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise ProviderError(f"Google Flights {what} failed (HTTP {response.status_code})")
    if "<title>Before you continue" in text:
        raise ProviderError("Google Flights shows its cookie consent page instead of results")
    return text


def google_calendar_search(body: str, currency: str) -> str:
    """The only place that sends calendar requests."""
    return _frontend_post(CALENDAR_URL, body, currency, "calendar query")


# Result list: the request behind Google Flights' list of flights, asked for every result
# sorted by price. A round trip lists outbound flights with the cheapest round-trip total;
# the return flights of one outbound flight need a second request that names it.
SHOPPING_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)
_SHOPPING_SEAT = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
_SORT_BY_PRICE = 2
_OUTBOUND_SEGMENT, _RETURN_SEGMENT = 3, 1
# Google answers dense requests now and then with an error; it works again a little later.
SHOPPING_RETRY_SECONDS = (4.0, 8.0, 12.0)

ShoppingPostFn = Callable[[str, str], str]


def shopping_request_body(request: FareRequest, outbound: list[SelectedLeg] | None = None) -> str:
    """Form body (`f.req=…`) for every itinerary sorted by price; with `outbound`, the return
    flights of exactly these outbound flights."""
    stops = _calendar_stops(request.max_stops)

    def segment(origin: str, destination: str, day: date, kind: int, selected=None) -> list:
        return (
            [[[[origin, 0]]], [[[destination, 0]]], None, stops, None, None, day.isoformat()]
            + [None, selected]
            + [None] * 5
            + [kind]
        )

    selected = (
        [
            [origin, day, destination, None, airline, number]
            for origin, day, destination, airline, number in outbound
        ]
        if outbound
        else None
    )
    segments = [
        segment(request.origin, request.destination, request.departure, _OUTBOUND_SEGMENT, selected)
    ]
    if request.return_date is not None:
        segments.append(
            segment(request.destination, request.origin, request.return_date, _RETURN_SEGMENT)
        )
    round_trip = request.return_date is not None
    settings = [None, None, _CALENDAR_ROUND_TRIP if round_trip else _CALENDAR_ONE_WAY, None, []]
    settings += [_SHOPPING_SEAT[request.seat], [request.adults, request.children, 0, 0]]
    settings += [None] * 6 + [segments, None, None, None, 1] + [None] * 10 + [0]
    filters = [[], settings, _SORT_BY_PRICE, 1, 0, 1]
    inner = json.dumps(filters, separators=(",", ":"))
    return "f.req=" + quote(json.dumps([None, inner], separators=(",", ":")))


def parse_shopping(text: str) -> list[FareOption]:
    """Priced itineraries of a GetShoppingResults response; ValueError if it has none at all
    (Google's intermittent error response)."""
    payloads = _wrb_payloads(text)
    if not payloads:
        raise ValueError("result data missing")
    try:
        return _fare_options(payloads[0])
    except (IndexError, TypeError) as exc:
        raise ValueError("unexpected result data") from exc


def selected_legs(details: dict | None) -> list[SelectedLeg] | None:
    """The flights of an itinerary in the form a return-flight request names them."""
    legs = []
    for segment in (details or {}).get("segments") or []:
        airline, _, number = (segment.get("flight") or "").partition(" ")
        if not (number and segment.get("from") and segment.get("to")):
            return None
        day = segment.get("departure_date")
        if not day:
            return None
        legs.append((segment["from"], day, segment["to"], airline, number))
    return legs or None


def google_shopping_post(body: str, currency: str) -> str:
    """The only place that sends result-list requests."""
    return _frontend_post(SHOPPING_URL, body, currency, "query")


def shopping_results(
    body: str,
    currency: str,
    *,
    post: ShoppingPostFn = google_shopping_post,
    sleep: Callable[[float], None] = time.sleep,
) -> list[FareOption]:
    """One result-list request, retried while Google answers with its error response."""
    for wait in (*SHOPPING_RETRY_SECONDS, None):
        try:
            return parse_shopping(post(body, currency))
        except ValueError:
            if wait is None:
                raise ProviderError("Google Flights result list has an unexpected format") from None
            sleep(wait)
    raise AssertionError("unreachable")


def google_flights_search(
    request: FareRequest,
    *,
    post: ShoppingPostFn = google_shopping_post,
    sleep: Callable[[float], None] = time.sleep,
    fallback: SearchFn | None = None,
) -> list[FareOption]:
    """Every result sorted by price; the result page if the result list keeps failing."""
    try:
        options = shopping_results(
            shopping_request_body(request), request.currency, post=post, sleep=sleep
        )
    except ProviderError as exc:
        log.info(
            "%s→%s: result list failed (%s), reading the page",
            request.origin,
            request.destination,
            exc,
        )
        return (fallback or fast_flights_search)(request)
    return _with_url(options, request)


def google_return_search(
    request: FareRequest,
    outbound: list[SelectedLeg],
    *,
    post: ShoppingPostFn = google_shopping_post,
    sleep: Callable[[float], None] = time.sleep,
) -> list[FareOption]:
    """Return flights for the given outbound flights; prices are round-trip totals."""
    body = shopping_request_body(request, outbound)
    return shopping_results(body, request.currency, post=post, sleep=sleep)


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
    calendar_days_per_request = CALENDAR_DAYS_PER_REQUEST
    supports_return_flights = True

    def __init__(
        self,
        *,
        request_delay_seconds: float = 3.0,
        search: SearchFn = google_flights_search,
        return_search: ReturnSearchFn = google_return_search,
        calendar_search: CalendarSearchFn = google_calendar_search,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._delay = request_delay_seconds
        self._search = search
        self._return_search = return_search
        self._calendar_search = calendar_search
        self._clock = clock
        self._sleep = sleep

    def _allowed(self, options: list[FareOption], query: PriceQuery) -> list[FareOption]:
        return [o for o in options if query.max_stops is None or o.stops <= query.max_stops]

    def _with_return(
        self, request: FareRequest, outbound: FareOption, query: PriceQuery
    ) -> FareOption:
        """The cheapest return flight of this outbound flight (one more request).

        Its price is the round-trip total of exactly this combination. Without a usable
        answer the outbound option stays as it is – it already carries the round-trip total.
        """
        legs = selected_legs(outbound.details)
        if legs is None:
            return outbound
        try:
            options = self._allowed(self._return_search(request, legs), query)
        except ProviderError as exc:
            log.warning("%s→%s return flights skipped: %s", query.origin, query.destination, exc)
            return outbound
        finally:
            self._sleep(self._delay)
        if not options:
            return outbound
        back = min(options, key=lambda o: o.price)
        details = dict(outbound.details or {})
        if back.details:
            details["return"] = {k: v for k, v in back.details.items() if k != "url"}
        return replace(outbound, price=back.price, details=details)

    def fetch_calendar(self, query: CalendarQuery) -> list[CalendarPrice]:
        """One request per 61 days; a failed chunk is skipped, all failing raises."""
        first_day = max(query.first_day, self._clock().date() + timedelta(days=1))
        chunks = calendar_chunks(first_day, query.last_day, CALENDAR_DAYS_PER_REQUEST)
        prices: list[CalendarPrice] = []
        errors: list[ProviderError] = []
        for start, end in chunks:
            try:
                text = self._calendar_search(
                    calendar_request_body(query, start, end), query.currency
                )
                if "<title>Before you continue" in text:
                    raise ProviderError(
                        "Google Flights shows its cookie consent page instead of results"
                    )
                found = parse_calendar(text, query.currency)
            except ProviderError as exc:
                errors.append(exc)
                continue
            except ValueError:
                errors.append(ProviderError("Google Flights calendar has an unexpected format"))
                continue
            finally:
                self._sleep(self._delay)
            prices.extend(p for p in found if start <= p.departure_date <= end)
        if chunks and len(errors) == len(chunks):
            raise errors[-1]
        return prices

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
            request = FareRequest(
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
            try:
                options = self._search(request)
            except ProviderError as exc:
                log.warning(
                    "%s→%s on %s skipped: %s", query.origin, query.destination, departure, exc
                )
                errors.append(exc)
                continue
            finally:
                self._sleep(self._delay)
            allowed = self._allowed(options, query)
            if not allowed:
                continue
            cheapest = min(allowed, key=lambda o: o.price)
            if query.return_flights and return_date is not None:
                cheapest = self._with_return(request, cheapest, query)
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
