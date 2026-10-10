"""Cheapest price per month and its previous-year counterpart."""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from flighttracker.services.history import Passengers, PricePoint, price_points


@dataclass(frozen=True)
class MonthKey:
    origin: str
    destination: str
    cabin_class: str
    currency: str
    month: date


@dataclass(frozen=True)
class MonthValue:
    """Cheapest price point of a departure month; `point` holds its flight (if known).

    `fake` is true when the price comes from a provider with invented data.
    """

    price: Decimal
    observed_at: datetime
    fake: bool = False
    point: PricePoint | None = None


@dataclass(frozen=True)
class MonthlyPriceRow:
    key: MonthKey
    current: MonthValue
    previous_year: MonthValue | None

    @property
    def fake(self) -> bool:
        return self.current.fake or (self.previous_year is not None and self.previous_year.fake)

    @property
    def change_percent(self) -> Decimal | None:
        if self.previous_year is None or self.previous_year.price == 0:
            return None
        change = (self.current.price - self.previous_year.price) / self.previous_year.price * 100
        return change.quantize(Decimal("0.1"))


def month_values(points: Iterable[PricePoint]) -> dict[MonthKey, MonthValue]:
    """Cheapest point per route, cabin, currency and departure month."""
    cheapest: dict[MonthKey, PricePoint] = {}
    for point in points:
        key = MonthKey(
            point.origin, point.destination, point.cabin_class, point.currency, point.month
        )
        if key not in cheapest or point.price < cheapest[key].price:
            cheapest[key] = point
    return {key: MonthValue(p.price, p.observed_at, p.fake, p) for key, p in cheapest.items()}


def _previous_year(key: MonthKey) -> MonthKey:
    return MonthKey(
        key.origin,
        key.destination,
        key.cabin_class,
        key.currency,
        key.month.replace(year=key.month.year - 1),
    )


def pair_with_previous_year(
    values: Mapping[MonthKey, MonthValue], today: date
) -> list[MonthlyPriceRow]:
    """Rows for all departure months from the current month on, with last year's value."""
    first_of_month = today.replace(day=1)
    rows = [
        MonthlyPriceRow(key, value, values.get(_previous_year(key)))
        for key, value in values.items()
        if key.month >= first_of_month
    ]
    return sorted(
        rows, key=lambda r: (r.key.month, r.key.origin, r.key.destination, r.key.cabin_class)
    )


def overview_from_points(points: Iterable[PricePoint], today: date) -> list[MonthlyPriceRow]:
    """Current months use upcoming departures only (a past date can no longer be booked);
    past departures provide the previous-year values."""
    points = list(points)
    this_month = today.replace(day=1)
    values = month_values(p for p in points if p.month < this_month)
    values.update(month_values(p for p in points if p.departure_date >= today))
    return pair_with_previous_year(values, today)


def monthly_overview(
    session: Session,
    search_id: int,
    today: date,
    fake_providers: Collection[str] = (),
    *,
    passengers: Passengers | None = None,
) -> list[MonthlyPriceRow]:
    points = price_points(session, search_id, fake_providers, passengers=passengers)
    return overview_from_points(points, today)
