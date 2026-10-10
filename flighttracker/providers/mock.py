import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from flighttracker.domain.filters import CabinClass, TripType
from flighttracker.providers.base import (
    CalendarPrice,
    CalendarQuery,
    FlightPriceProvider,
    PriceQuery,
    PriceQuote,
    return_dates,
)

_CABIN_FACTOR = {
    CabinClass.ECONOMY: 1.0,
    CabinClass.PREMIUM_ECONOMY: 1.8,
    CabinClass.BUSINESS: 3.5,
    CabinClass.FIRST: 6.0,
}
# Rough seasonality, Jan..Dec.
_SEASON_FACTOR = [0.85, 0.8, 0.9, 1.0, 1.05, 1.2, 1.4, 1.35, 1.05, 0.95, 0.85, 1.15]
_SAMPLE_DAYS = (3, 10, 17, 24)


def _hash_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


class MockProvider(FlightPriceProvider):
    """Deterministic fake prices for development and tests – NOT real data."""

    name = "mock"
    supports_children = True
    fake_data = True
    supports_connection_times = True
    calendar_days_per_request = 61

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._clock = clock

    def fetch_calendar(self, query: CalendarQuery) -> list[CalendarPrice]:
        now = self._clock()
        price_query = PriceQuery(
            origin=query.origin,
            destination=query.destination,
            departure_month=query.first_day.replace(day=1),
            cabin_class=query.cabin_class,
            trip_type=TripType.ONE_WAY if query.stay_days is None else TripType.ROUND_TRIP,
            max_stops=query.max_stops,
            stay_days_min=query.stay_days,
            stay_days_max=query.stay_days,
            adults=query.adults,
            children=query.children,
            currency=query.currency,
        )
        prices = []
        day = max(query.first_day, now.date() + timedelta(days=1))
        while day <= query.last_day:
            back = day + timedelta(days=query.stay_days) if query.stay_days is not None else None
            quote = self._quote(price_query, day, back, observed_at=now)
            prices.append(CalendarPrice(day, back, quote.price, query.currency))
            day += timedelta(days=1)
        return prices

    def fetch_current(self, query: PriceQuery) -> list[PriceQuote]:
        now = self._clock()
        days = (
            query.departure_dates
            if query.departure_dates is not None
            else self._departures(query.departure_month)
        )
        return [
            self._quote(query, departure, return_date, observed_at=now)
            for departure in days
            if departure > now.date()
            for return_date in return_dates(query, departure)
        ]

    @staticmethod
    def _departures(month: date) -> list[date]:
        return [month.replace(day=day) for day in _SAMPLE_DAYS]

    def _quote(
        self,
        query: PriceQuery,
        departure: date,
        return_date: date | None,
        *,
        observed_at: datetime,
    ) -> PriceQuote:
        route_base = 80 + _hash_int(query.origin, query.destination) % 400
        jitter = (
            _hash_int(query.origin, query.destination, departure, return_date, observed_at.date())
            % 40
        )
        days_before = max((departure - observed_at.date()).days, 0)
        lead_time_factor = 1 + max(0, 60 - days_before) / 200
        price = (
            (route_base + jitter)
            * _CABIN_FACTOR[query.cabin_class]
            * _SEASON_FACTOR[departure.month - 1]
            * lead_time_factor
        )
        if query.trip_type is TripType.ROUND_TRIP:
            price *= 1.8
        price *= query.adults + 0.75 * query.children
        return PriceQuote(
            provider=self.name,
            origin=query.origin,
            destination=query.destination,
            departure_date=departure,
            return_date=return_date,
            cabin_class=query.cabin_class,
            stops=0 if query.max_stops == 0 else _hash_int(query.origin, departure) % 2,
            price=Decimal(str(round(price, 2))),
            currency=query.currency,
            observed_at=observed_at,
            airline="XX",
            details={
                "departure_time": "10:00",
                "arrival_date": departure.isoformat(),
                "arrival_time": "12:00",
            },
        )
