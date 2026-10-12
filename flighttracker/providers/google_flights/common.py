"""Shared parts of the Google Flights adapter: request/answer types, the HTTP client with the
consent cookie, and reading itineraries (same structure on the result page and in the result
list)."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from flighttracker.domain.filters import CabinClass
from flighttracker.providers.base import ProviderError

URL = "https://www.google.com/travel/flights"
# "Reject all" consent – without it, EU/CH requests only get the "Before you continue" page.
CONSENT_COOKIE = "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiAo_CmBg"

SEAT = {
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

# Trip type and stop filter codes of Google's FlightsFrontendService requests.
ROUND_TRIP_CODE, ONE_WAY_CODE = 1, 2


def stops_code(max_stops: int | None) -> int:
    """Google's stop filter: 0 any, 1 non-stop, 2 at most one stop, 3 at most two stops."""
    if max_stops is None or max_stops >= 3:
        return 0
    return max_stops + 1


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


def fare_options(payload: list) -> list[FareOption]:
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


def http_client():
    try:
        from primp import Client  # HTTP client of fast-flights (browser impersonation)
    except ImportError as exc:
        raise ProviderError("Package fast-flights missing: pip install '.[scraper]'") from exc
    return Client(impersonate="chrome_145", impersonate_os="macos", referer=True)


def wrb_payloads(text: str) -> list:
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


def frontend_post(url: str, body: str, currency: str, what: str) -> str:
    """POST to a FlightsFrontendService endpoint (same client and consent cookie as the page)."""
    client = http_client()
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
