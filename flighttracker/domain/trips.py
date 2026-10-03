from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Protocol


class TripLegQuote(Protocol):
    origin: str
    destination: str
    cabin_class: str
    currency: str
    departure_date: date
    price: Decimal
    details: dict | None
    fake: bool


@dataclass(frozen=True)
class LayoverRule:
    minimum_days: int | None = None
    maximum_days: int | None = None


@dataclass(frozen=True)
class TripItinerary:
    legs: tuple[TripLegQuote, ...]
    total_price: Decimal
    currency: str

    @property
    def fake(self) -> bool:
        return any(getattr(leg, "fake", False) for leg in self.legs)


def _local_datetime(day: date, value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    return datetime.combine(day, parsed)


def departure_at(point: TripLegQuote) -> datetime | None:
    details = point.details or {}
    return _local_datetime(point.departure_date, details.get("departure_time"))


def arrival_at(point: TripLegQuote) -> datetime | None:
    details = point.details or {}
    arrival_date = details.get("arrival_date")
    if not arrival_date:
        segments = details.get("segments") or []
        if segments:
            arrival_date = segments[-1].get("arrival_date")
            arrival_time = segments[-1].get("arrival_time")
        else:
            arrival_time = None
    else:
        arrival_time = details.get("arrival_time")
    try:
        day = date.fromisoformat(arrival_date) if arrival_date else None
    except ValueError:
        return None
    return _local_datetime(day, arrival_time) if day else None


def layover_days(previous: TripLegQuote, following: TripLegQuote) -> int | None:
    """Calendar-date difference at the connecting airport; times still establish order."""
    if previous.destination != following.origin:
        return None
    arrival = arrival_at(previous)
    departure = departure_at(following)
    if arrival is None or departure is None or departure < arrival:
        return None
    return (departure.date() - arrival.date()).days


def connection_fits(previous: TripLegQuote, following: TripLegQuote, rule: LayoverRule) -> bool:
    days = layover_days(previous, following)
    if days is None:
        return False
    if rule.minimum_days is not None and days < rule.minimum_days:
        return False
    return rule.maximum_days is None or days <= rule.maximum_days


def find_itineraries(
    points_by_leg: list[list[TripLegQuote]],
    layover_rules: list[LayoverRule],
    starts_on: date,
    ends_on: date,
    *,
    limit: int = 200,
) -> list[TripItinerary]:
    """Join latest leg prices into complete, time-feasible trips within the date window.

    Layover rules have one entry per transition (one fewer than the number of legs). A quote
    without usable departure or arrival times cannot prove that a connection will work.
    """
    if len(points_by_leg) < 2 or len(layover_rules) != len(points_by_leg) - 1:
        return []
    results: list[TripItinerary] = []

    def extend(path: tuple[TripLegQuote, ...], leg_index: int) -> None:
        if len(results) >= limit:
            return
        if leg_index == len(points_by_leg):
            arrival = arrival_at(path[-1])
            if arrival is None or arrival.date() > ends_on:
                return
            currencies = {point.currency for point in path}
            if len(currencies) != 1:
                return
            results.append(
                TripItinerary(
                    legs=path,
                    total_price=sum((point.price for point in path), Decimal(0)),
                    currency=path[0].currency,
                )
            )
            return

        previous = path[-1]
        rule = layover_rules[leg_index - 1]
        for point in points_by_leg[leg_index]:
            if point.departure_date < starts_on or point.departure_date > ends_on:
                continue
            if connection_fits(previous, point, rule):
                extend((*path, point), leg_index + 1)

    for point in points_by_leg[0]:
        departure = departure_at(point)
        if departure is None or not starts_on <= departure.date() <= ends_on:
            continue
        if arrival_at(point) is not None:
            extend((point,), 1)

    return sorted(results, key=lambda itinerary: itinerary.total_price)
