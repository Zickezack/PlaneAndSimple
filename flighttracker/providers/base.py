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

    @abstractmethod
    def fetch_current(self, query: PriceQuery) -> list[PriceQuote]:
        """Current prices for departures in `query.departure_month`."""


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
