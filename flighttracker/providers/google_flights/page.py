"""Fallback: fares from the Google Flights result page (only the ~10 flights it shows without
"more flights"). fast-flights (pinned in pyproject.toml) builds the page URL."""

import json
import re
from dataclasses import replace
from urllib.parse import urlencode

from flighttracker.providers.base import ProviderError
from flighttracker.providers.google_flights.common import (
    CONSENT_COOKIE,
    URL,
    FareOption,
    FareRequest,
    fare_options,
    http_client,
)

_RESULTS_SCRIPT = re.compile(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', re.S)


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
    return fare_options(json.loads(data))


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


def with_url(options: list[FareOption], request: FareRequest) -> list[FareOption]:
    """Link to exactly this search on Google Flights (for checking and booking)."""
    url = f"{URL}?{urlencode(_page_query(request).params())}"
    return [replace(o, details={**o.details, "url": url}) if o.details else o for o in options]


def fast_flights_search(request: FareRequest) -> list[FareOption]:
    """Fares from the result page (only the flights it shows without "more flights")."""
    query = _page_query(request)
    try:
        html = (
            http_client().get(URL, params=query.params(), headers={"Cookie": CONSENT_COOKIE}).text
        )
        if "<title>Before you continue" in html:
            raise ProviderError("Google Flights shows its cookie consent page instead of results")
        return with_url(parse_fares(html), request)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Google Flights query failed ({type(exc).__name__})") from None
