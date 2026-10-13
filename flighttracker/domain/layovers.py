"""Layovers of an itinerary and the warnings worth showing next to a flight.

Computed from the stored segments (`price_history.details`), so they apply to every price
that has flight details, old ones included. Segment times are local airport times; a layover
stays in one city, so the difference of arrival and next departure is its real length.
"""

from dataclasses import dataclass
from datetime import datetime

# Below this a missed connection is likely (Google Flights shows no such warning).
SHORT_MINUTES = 60
# Changing airports (e.g. Heathrow → Gatwick) needs a transfer across the city.
SHORT_AIRPORT_CHANGE_MINUTES = 180
# Google Flights marks layovers from about 5 hours as long.
LONG_MINUTES = 5 * 60

AIRPORT_CHANGE = "airport_change"
SHORT = "short"
LONG = "long"
OVERNIGHT = "overnight"
# Order in which warnings are shown: the ones that can cost the connection first.
WARNINGS = (AIRPORT_CHANGE, SHORT, OVERNIGHT, LONG)


@dataclass(frozen=True)
class Layover:
    arrival_airport: str
    departure_airport: str
    minutes: int | None
    # The next flight leaves on a later (local) day than the previous one arrived.
    overnight: bool

    @property
    def airport_change(self) -> bool:
        return self.arrival_airport != self.departure_airport

    @property
    def warnings(self) -> tuple[str, ...]:
        found = set()
        if self.airport_change:
            found.add(AIRPORT_CHANGE)
        if self.minutes is not None:
            limit = SHORT_AIRPORT_CHANGE_MINUTES if self.airport_change else SHORT_MINUTES
            if self.minutes < limit:
                found.add(SHORT)
            if self.minutes >= LONG_MINUTES:
                found.add(LONG)
        if self.overnight:
            found.add(OVERNIGHT)
        return tuple(warning for warning in WARNINGS if warning in found)


def _moment(day: str | None, time: str | None) -> datetime | None:
    if not day or not time:
        return None
    try:
        return datetime.fromisoformat(f"{day}T{time}")
    except ValueError:
        return None


def layovers(segments: list[dict]) -> list[Layover]:
    """One layover between each two consecutive segments."""
    result = []
    for arriving, leaving in zip(segments, segments[1:], strict=False):
        arrival = _moment(arriving.get("arrival_date"), arriving.get("arrival_time"))
        departure = _moment(leaving.get("departure_date"), leaving.get("departure_time"))
        result.append(
            Layover(
                arrival_airport=arriving.get("to") or "?",
                departure_airport=leaving.get("from") or arriving.get("to") or "?",
                minutes=int((departure - arrival).total_seconds() // 60)
                if arrival and departure
                else None,
                overnight=bool(arrival and departure and departure.date() > arrival.date()),
            )
        )
    return result


def itinerary_warnings(details: dict | None) -> tuple[str, ...]:
    """Every warning of the outbound and (if loaded) the return flight, each once."""
    found = set()
    for part in (details or {}, (details or {}).get("return") or {}):
        for layover in layovers(part.get("segments") or []):
            found.update(layover.warnings)
    return tuple(warning for warning in WARNINGS if warning in found)
