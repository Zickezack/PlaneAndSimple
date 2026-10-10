from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from flighttracker.domain.filters import CabinClass, TripType, stay_lengths


@dataclass(frozen=True)
class PriceQuery:
    """One provider request: a single route, cabin class and departure month."""

    origin: str
    destination: str
    departure_month: date
    cabin_class: CabinClass
    trip_type: TripType
    max_stops: int | None
    stay_days_min: int | None
    stay_days_max: int | None
    adults: int
    currency: str
    children: int = 0
    days_per_month: int = 4
    departure_dates: tuple[date, ...] | None = None


@dataclass(frozen=True)
class PriceQuote:
    provider: str
    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    cabin_class: CabinClass
    stops: int | None
    price: Decimal
    currency: str
    observed_at: datetime
    airline: str | None = None
    # Flight details for the UI, see `FlightDetails` in datamodel.md (None if unknown).
    details: dict | None = None


@dataclass(frozen=True)
class CalendarQuery:
    """Cheapest price per departure day over a date range – one route, cabin and stay length.

    `stay_days` None means one-way. Providers split long ranges into several requests.
    """

    origin: str
    destination: str
    cabin_class: CabinClass
    first_day: date
    last_day: date
    stay_days: int | None
    max_stops: int | None
    adults: int
    currency: str
    children: int = 0


@dataclass(frozen=True)
class CalendarPrice:
    """The cheapest price of one departure day (and its return day for round trips).

    Only a price – the source does not say which flight it belongs to.
    """

    departure_date: date
    return_date: date | None
    price: Decimal
    currency: str


class ProviderError(Exception):
    """Raised by adapters for failed requests.

    Messages end up in `fetch_jobs.last_error` – never include tokens or full URLs.
    """


class FlightPriceProvider(ABC):
    name: ClassVar[str]
    # Capabilities, shown as notes in the UI when a Suchabo asks for more than the source can do.
    supports_children: ClassVar[bool] = False
    # False: prices are for a single adult, whatever the number of passengers.
    prices_all_passengers: ClassVar[bool] = True
    # True: the prices are invented (demo/test data) and must be marked as such in the UI.
    fake_data: ClassVar[bool] = False
    # True: one request per departure day and stay length (cost = days × stays per query).
    samples_days: ClassVar[bool] = False
    # True when quote details include arrival and departure times for connection checks.
    supports_connection_times: ClassVar[bool] = False
    # Price calendar (heat map): days covered by one request; None = not supported.
    calendar_days_per_request: ClassVar[int | None] = None

    @abstractmethod
    def fetch_current(self, query: PriceQuery) -> list[PriceQuote]:
        """Current prices for departures in `query.departure_month`."""

    def fetch_calendar(self, query: CalendarQuery) -> list[CalendarPrice]:
        """Cheapest price per departure day (providers with `calendar_days_per_request`)."""
        raise NotImplementedError(f"{self.name} has no price calendar")


def calendar_chunks(first_day: date, last_day: date, days: int) -> list[tuple[date, date]]:
    """`first_day`..`last_day` split into ranges of at most `days` days."""
    chunks = []
    start = first_day
    while start <= last_day:
        end = min(start + timedelta(days=days - 1), last_day)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def return_dates(query: PriceQuery, departure: date) -> list[date | None]:
    """Return dates to query for one departure day (`[None]` for one-way trips)."""
    lengths = stay_lengths(query.trip_type, query.stay_days_min, query.stay_days_max)
    return [departure + timedelta(days=days) for days in lengths] or [None]


def stay_matches(query: PriceQuery, departure: date, return_date: date | None) -> bool:
    """Shared post-filter for providers that cannot filter by length of stay themselves."""
    if query.trip_type is TripType.ONE_WAY:
        return return_date is None
    if return_date is None:
        return False
    stay = (return_date - departure).days
    if query.stay_days_min is not None and stay < query.stay_days_min:
        return False
    return query.stay_days_max is None or stay <= query.stay_days_max
