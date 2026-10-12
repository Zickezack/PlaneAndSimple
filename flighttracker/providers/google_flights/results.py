"""Result list: the request behind Google Flights' list of flights, asked for every result
sorted by price. A round trip lists outbound flights with the cheapest round-trip total; the
return flights of one outbound flight need a second request that names it."""

import json
import logging
import time
from collections.abc import Callable
from datetime import date
from urllib.parse import quote

from flighttracker.providers.base import ProviderError
from flighttracker.providers.google_flights.common import (
    ONE_WAY_CODE,
    ROUND_TRIP_CODE,
    FareOption,
    FareRequest,
    SearchFn,
    SelectedLeg,
    fare_options,
    frontend_post,
    stops_code,
    wrb_payloads,
)
from flighttracker.providers.google_flights.page import fast_flights_search, with_url

log = logging.getLogger(__name__)

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
    stops = stops_code(request.max_stops)

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
    settings = [None, None, ROUND_TRIP_CODE if round_trip else ONE_WAY_CODE, None, []]
    settings += [_SHOPPING_SEAT[request.seat], [request.adults, request.children, 0, 0]]
    settings += [None] * 6 + [segments, None, None, None, 1] + [None] * 10 + [0]
    filters = [[], settings, _SORT_BY_PRICE, 1, 0, 1]
    inner = json.dumps(filters, separators=(",", ":"))
    return "f.req=" + quote(json.dumps([None, inner], separators=(",", ":")))


def parse_shopping(text: str) -> list[FareOption]:
    """Priced itineraries of a GetShoppingResults response; ValueError if it has none at all
    (Google's intermittent error response)."""
    payloads = wrb_payloads(text)
    if not payloads:
        raise ValueError("result data missing")
    try:
        return fare_options(payloads[0])
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
    return frontend_post(SHOPPING_URL, body, currency, "query")


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
    return with_url(options, request)


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
