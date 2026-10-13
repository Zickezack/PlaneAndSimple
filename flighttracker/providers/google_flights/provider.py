"""The Google Flights provider: fares per sampled day and stay length, return flights and the
price calendar, with a polite pause after every request."""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial

from flighttracker.domain.sampling import sample_days
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
from flighttracker.providers.google_flights.common import (
    SEAT,
    FareOption,
    FareRequest,
    ReturnSearchFn,
    SearchFn,
    with_retries,
)
from flighttracker.providers.google_flights.price_calendar import (
    CALENDAR_DAYS_PER_REQUEST,
    CalendarSearchFn,
    calendar_request_body,
    google_calendar_search,
    parse_calendar,
)
from flighttracker.providers.google_flights.results import (
    google_flights_search,
    google_return_search,
    selected_legs,
)

log = logging.getLogger(__name__)


def _calendar_prices(text: str, currency: str) -> list[CalendarPrice]:
    """ValueError (retried) for an unusable answer; the consent page is not worth a retry."""
    if "<title>Before you continue" in text:
        raise ProviderError("Google Flights shows its cookie consent page instead of results")
    return parse_calendar(text, currency)


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
            body = calendar_request_body(query, start, end)
            try:
                found = with_retries(
                    partial(self._calendar_search, body, query.currency),
                    partial(_calendar_prices, currency=query.currency),
                    sleep=self._sleep,
                    what="calendar",
                )
            except ProviderError as exc:
                errors.append(exc)
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
                seat=SEAT[query.cabin_class],
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
