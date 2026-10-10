"""Read side of the price history: price points per departure date and the month overview.

A *price point* is the current price for one route, cabin, departure and return date: the
latest observation (whatever the stops – Google reports the day's cheapest flight, which may
switch between direct and connecting). Several stay lengths give several points per day.

Prices for different passengers are not comparable (Google Flights prices all passengers
together), so the readers below take the passengers to show (`passengers=(adults, children)`)
and ignore observations for other passenger numbers – a changed passenger number never
shows up as a price change.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
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


def observation_counts(session: Session, search_ids: list[int]) -> dict[int, int]:
    if not search_ids:
        return {}
    rows = session.execute(
        select(PriceHistory.search_id, func.count())
        .where(PriceHistory.search_id.in_(search_ids))
        .group_by(PriceHistory.search_id)
    )
    return {key: value for key, value in rows}
