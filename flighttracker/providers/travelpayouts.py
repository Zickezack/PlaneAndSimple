"""Travelpayouts (Aviasales) Data API adapter.

Free with a Travelpayouts account. Prices are *cached* results of Aviasales user searches
(`found_at` = when the price was seen, usually within the last days), not live fares.
Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from flighttracker.domain.filters import CabinClass, TripType
from flighttracker.providers.base import (
    FlightPriceProvider,
    PriceQuery,
    PriceQuote,
    ProviderError,
    stay_matches,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.travelpayouts.com"
MONTH_MATRIX_PATH = "/v2/prices/month-matrix"

# Travelpayouts `trip_class`: 0 = economy, 1 = business, 2 = first. No premium economy.
_TRIP_CLASS = {0: CabinClass.ECONOMY, 1: CabinClass.BUSINESS, 2: CabinClass.FIRST}


class TravelpayoutsProvider(FlightPriceProvider):
    name = "travelpayouts"
    # month-matrix prices are for one adult; passenger counts cannot be requested.
    prices_all_passengers = False

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        min_request_interval_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise ValueError("TRAVELPAYOUTS_TOKEN is not set.")
        # Token goes into a header so it never appears in URLs, logs or error messages.
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=20.0)
        self._client.headers["X-Access-Token"] = token
        self._min_interval = min_request_interval_seconds
        self._sleep = sleep
        self._last_request = 0.0

    def fetch_current(self, query: PriceQuery) -> list[PriceQuote]:
        params = {
            "origin": query.origin,
            "destination": query.destination,
            "month": query.departure_month.isoformat(),
            "currency": query.currency.lower(),
            "one_way": "true" if query.trip_type is TripType.ONE_WAY else "false",
            "show_to_affiliates": "true",
        }
        payload = self._get(MONTH_MATRIX_PATH, params)
        return [
            quote
            for entry in payload.get("data") or []
            if (quote := self._to_quote(entry, query)) is not None
        ]

    def _get(self, path: str, params: dict[str, str]) -> dict:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Travelpayouts unreachable ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ProviderError(f"Travelpayouts answered with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("Travelpayouts answered with something other than JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("Travelpayouts answered with an unexpected JSON shape")
        if not payload.get("success", False):
            raise ProviderError(f"Travelpayouts error: {str(payload.get('error'))[:200]}")
        return payload

    def _to_quote(self, entry: dict, query: PriceQuery) -> PriceQuote | None:
        try:
            cabin = _TRIP_CLASS.get(int(entry.get("trip_class", 0)))
            departure = date.fromisoformat(entry["depart_date"])
            return_date = (
                date.fromisoformat(entry["return_date"]) if entry.get("return_date") else None
            )
            stops = int(entry["number_of_changes"])
            price = Decimal(str(entry["value"]))
            observed_at = datetime.fromisoformat(entry["found_at"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            log.warning("Travelpayouts: incomplete entry skipped")
            return None
        if cabin is not query.cabin_class:
            return None
        if query.max_stops is not None and stops > query.max_stops:
            return None
        if not stay_matches(query, departure, return_date):
            return None
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        return PriceQuote(
            provider=self.name,
            # Keep our airport codes; the API may answer with city codes (e.g. MIL).
            origin=query.origin,
            destination=query.destination,
            departure_date=departure,
            return_date=return_date,
            cabin_class=cabin,
            stops=stops,
            price=price,
            currency=query.currency,
            observed_at=observed_at,
        )
