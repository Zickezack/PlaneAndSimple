"""Read side of the price history: price points per departure date and the month overview.

A *price point* is the current price for one route, cabin, departure and return date: the
latest observation (whatever the stops – Google reports the day's cheapest flight, which may
switch between direct and connecting). Several stay lengths give several points per day.

Prices for different passengers are not comparable (Google Flights prices all passengers
together), so the readers below take the passengers to show (`passengers=(adults, children)`)
and ignore observations for other passenger numbers – a changed passenger number never
shows up as a price change.
"""

from calendar import monthrange
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from flighttracker.models import CalendarObservation, PriceHistory


@dataclass(frozen=True)
class PricePoint:
    origin: str
    destination: str
    cabin_class: str
    currency: str
    departure_date: date
    return_date: date | None
    stops: int | None
    price: Decimal
    # Price of the same flight dates at the poll before (None on the first observation).
    previous_price: Decimal | None
    airline: str | None
    details: dict | None
    observed_at: datetime
    provider: str
    fake: bool
    # Id of the price_history row behind this point (for the flight detail page).
    id: int | None = None

    @property
    def month(self) -> date:
        return self.departure_date.replace(day=1)

    @property
    def stay_days(self) -> int | None:
        return (self.return_date - self.departure_date).days if self.return_date else None

    @property
    def change(self) -> Decimal | None:
        return None if self.previous_price is None else self.price - self.previous_price


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


_PRICE_POINTS = text(
    """
    WITH ranked AS (
        SELECT id, origin_iata, destination_iata, cabin_class, currency, departure_date,
               return_date, stops, price, airline, details, observed_at, provider,
               row_number() OVER flight AS rn,
               lead(price) OVER flight AS previous_price
          FROM price_history
         WHERE search_id = :search_id
           AND (CAST(:adults AS smallint) IS NULL OR adults = :adults)
           AND (CAST(:children AS smallint) IS NULL OR children = :children)
        WINDOW flight AS (
            PARTITION BY origin_iata, destination_iata, cabin_class, currency,
                         departure_date, return_date
            ORDER BY observed_at DESC, id DESC
        )
    )
    SELECT *, provider = ANY(:fake_providers) AS fake
      FROM ranked
     WHERE rn = 1
    """
)


Passengers = tuple[int, int]


def price_points(
    session: Session,
    search_id: int,
    fake_providers: Collection[str] = (),
    *,
    passengers: Passengers | None = None,
) -> list[PricePoint]:
    """All price points of a Suchabo (past departures included), sorted by date and price.

    `passengers`: only observations for these (adults, children); None = all.
    """
    adults, children = passengers if passengers is not None else (None, None)
    rows = session.execute(
        _PRICE_POINTS,
        {
            "search_id": search_id,
            "fake_providers": list(fake_providers),
            "adults": adults,
            "children": children,
        },
    )
    points = [
        PricePoint(
            origin=r.origin_iata,
            destination=r.destination_iata,
            cabin_class=r.cabin_class,
            currency=r.currency,
            departure_date=r.departure_date,
            return_date=r.return_date,
            stops=r.stops,
            price=r.price,
            previous_price=r.previous_price,
            airline=r.airline,
            details=r.details,
            observed_at=r.observed_at,
            provider=r.provider,
            fake=bool(r.fake),
            id=r.id,
        )
        for r in rows
    ]
    return sorted(points, key=lambda p: (p.departure_date, p.origin, p.destination, p.price))


@dataclass(frozen=True)
class CalendarDay:
    """Latest price-calendar price of one departure day (and stay) – no flight behind it."""

    origin: str
    destination: str
    cabin_class: str
    currency: str
    departure_date: date
    return_date: date | None
    price: Decimal
    fake: bool

    @property
    def stay_days(self) -> int | None:
        return (self.return_date - self.departure_date).days if self.return_date else None


def calendar_days(
    session: Session,
    search_id: int,
    since: date,
    fake_providers: Collection[str] = (),
    *,
    passengers: Passengers,
) -> list[CalendarDay]:
    """Latest calendar price per route, cabin, currency and departure/return day from `since`."""
    c = CalendarObservation
    key = (c.origin_iata, c.destination_iata, c.cabin_class, c.currency, c.departure_date)
    rows = session.execute(
        select(*key, c.return_date, c.price, c.provider)
        .where(
            c.search_id == search_id,
            c.departure_date >= since,
            c.adults == passengers[0],
            c.children == passengers[1],
        )
        .distinct(*key, c.return_date)
        .order_by(*key, c.return_date, c.observed_at.desc(), c.id.desc())
    )
    return [
        CalendarDay(
            origin=r.origin_iata,
            destination=r.destination_iata,
            cabin_class=str(r.cabin_class),
            currency=r.currency,
            departure_date=r.departure_date,
            return_date=r.return_date,
            price=r.price,
            fake=r.provider in fake_providers,
        )
        for r in rows
    ]


def observation(session: Session, search_id: int, price_id: int) -> PriceHistory | None:
    return session.scalars(
        select(PriceHistory).where(PriceHistory.id == price_id, PriceHistory.search_id == search_id)
    ).first()


def other_passenger_prices(
    session: Session, search_id: int, passengers: Passengers, since_departure: date
) -> int:
    """Observations of upcoming departures recorded for other passenger numbers (hidden)."""
    adults, children = passengers
    return session.scalar(
        select(func.count()).where(
            PriceHistory.search_id == search_id,
            PriceHistory.departure_date >= since_departure,
            (PriceHistory.adults != adults) | (PriceHistory.children != children),
        )
    )


def flight_history(session: Session, observed: PriceHistory) -> list[PriceHistory]:
    """Every poll's price for the same route, cabin, currency, passengers and flight dates,
    newest first."""
    return list(
        session.scalars(
            select(PriceHistory)
            .where(
                PriceHistory.search_id == observed.search_id,
                PriceHistory.origin_iata == observed.origin_iata,
                PriceHistory.destination_iata == observed.destination_iata,
                PriceHistory.cabin_class == observed.cabin_class,
                PriceHistory.currency == observed.currency,
                PriceHistory.adults == observed.adults,
                PriceHistory.children == observed.children,
                PriceHistory.departure_date == observed.departure_date,
                PriceHistory.return_date.is_not_distinct_from(observed.return_date),
            )
            .order_by(PriceHistory.observed_at.desc(), PriceHistory.id.desc())
        )
    )


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


def observation_counts(session: Session, search_ids: list[int]) -> dict[int, int]:
    if not search_ids:
        return {}
    rows = session.execute(
        select(PriceHistory.search_id, func.count())
        .where(PriceHistory.search_id.in_(search_ids))
        .group_by(PriceHistory.search_id)
    )
    return {key: value for key, value in rows}
