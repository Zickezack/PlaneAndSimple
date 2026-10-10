"""Price development of single flights: every observation, first price, 1d/1w/1m changes."""

from calendar import monthrange
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from flighttracker.models import PriceHistory
from flighttracker.services.history import Passengers, PricePoint

FlightKey = tuple[str, str, str, str, date, date | None]


@dataclass(frozen=True)
class PriceObservation:
    price: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class FlightChange:
    period: str
    amount: Decimal


def flight_key(point: PricePoint) -> FlightKey:
    return (
        point.origin,
        point.destination,
        str(point.cabin_class),
        point.currency,
        point.departure_date,
        point.return_date,
    )


def price_series(
    session: Session, search_id: int, since_departure: date, *, passengers: Passengers | None = None
) -> dict[FlightKey, list[PriceObservation]]:
    """Every observed price per flight (oldest first) for departures from `since_departure` on."""
    statement = (
        select(
            PriceHistory.origin_iata,
            PriceHistory.destination_iata,
            PriceHistory.cabin_class,
            PriceHistory.currency,
            PriceHistory.departure_date,
            PriceHistory.return_date,
            PriceHistory.price,
            PriceHistory.observed_at,
        )
        .where(PriceHistory.search_id == search_id, PriceHistory.departure_date >= since_departure)
        .order_by(PriceHistory.observed_at, PriceHistory.id)
    )
    if passengers is not None:
        statement = statement.where(
            PriceHistory.adults == passengers[0], PriceHistory.children == passengers[1]
        )
    rows = session.execute(statement)
    series: dict[FlightKey, list[PriceObservation]] = {}
    for origin, destination, cabin, currency, departure, return_date, price, observed_at in rows:
        key = (origin, destination, str(cabin), currency, departure, return_date)
        series.setdefault(key, []).append(PriceObservation(price, observed_at))
    return series


def _one_month_before(value: datetime) -> datetime:
    month = value.month - 1 or 12
    year = value.year if value.month > 1 else value.year - 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _period_changes(
    point: PricePoint, values: tuple[PriceObservation, ...]
) -> tuple[FlightChange, ...]:
    periods = (
        ("1d", point.observed_at - timedelta(days=1), timedelta(hours=36)),
        ("1w", point.observed_at - timedelta(days=7), timedelta(days=10, hours=12)),
        ("1m", _one_month_before(point.observed_at), timedelta(days=45)),
    )
    changes = []
    for label, target, max_age in periods:
        prior = next(
            (
                observation
                for observation in reversed(values[:-1])
                if observation.observed_at <= target
            ),
            None,
        )
        if prior is None or target - prior.observed_at > max_age:
            continue
        changes.append(FlightChange(label, point.price - prior.price))
    return tuple(changes)


@dataclass(frozen=True)
class FlightTrend:
    """How one flight's price moved from its first observation to now (needs two or more)."""

    point: PricePoint
    values: tuple[PriceObservation, ...]

    @property
    def first(self) -> Decimal:
        return self.values[0].price

    @property
    def current(self) -> Decimal:
        return self.values[-1].price

    @property
    def change(self) -> Decimal:
        return self.current - self.first

    @property
    def percent(self) -> Decimal:
        if self.first == 0:
            return Decimal(0)
        return (self.change / self.first * 100).quantize(Decimal("0.1"))

    @property
    def changes(self) -> tuple[FlightChange, ...]:
        return _period_changes(self.point, self.values)


def flight_trends(
    points: Iterable[PricePoint], series: Mapping[FlightKey, list[PriceObservation]]
) -> dict[FlightKey, FlightTrend]:
    trends = {}
    for point in points:
        key = flight_key(point)
        values = series.get(key, [])
        if len(values) >= 2:
            trends[key] = FlightTrend(point, tuple(values))
    return trends
