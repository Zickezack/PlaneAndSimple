"""What a completed poll of a tracked search changed, compared with the poll before – and
whether that is worth telling ("notable").

Pure and deterministic: every number is the app's. The report is shown as it is (page, list,
mails); an AI model may put a notable one into a few sentences (services/ai_summaries.py) but
never changes it. Stored as JSON (`poll_reports.facts`, `to_facts` / `from_facts`).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from flighttracker.domain.booking_advice import TREND_SHARE
from flighttracker.domain.deals import Deal

# A change of at least this share counts: cheaper or dearer, the cheapest price, a new lowest.
CHANGE_SHARE = TREND_SHARE
# A poll is notable when at least this share of the compared flights changed.
MANY_CHANGED_SHARE = Decimal("0.25")
# How many flights each list keeps (new lowest prices, drops, rises).
TOP = 3
FACTS_VERSION = 1


@dataclass(frozen=True)
class Flight:
    origin: str
    destination: str
    cabin: str
    departure: date
    returning: date | None
    price: Decimal
    previous: Decimal | None = None  # at the poll before; None: first seen in this poll
    lowest_before: Decimal | None = None  # lowest of all earlier observations

    @property
    def key(self) -> tuple[str, str, str, date, date | None]:
        return (self.origin, self.destination, self.cabin, self.departure, self.returning)

    @property
    def change(self) -> Decimal | None:
        return None if self.previous is None else self.price - self.previous


@dataclass(frozen=True)
class MonthPrice:
    month: date
    price: Decimal
    previous: Decimal | None


@dataclass(frozen=True)
class SearchReport:
    currency: str
    flights: int  # seen in this poll
    compared: int  # of them with a price at the poll before
    cheaper: int  # by CHANGE_SHARE or more
    dearer: int
    cheapest: Flight | None
    cheapest_before: Decimal | None  # the cheapest price at the poll before
    months: tuple[MonthPrice, ...] = ()
    new_low_count: int = 0
    new_lows: tuple[Flight, ...] = ()  # CHANGE_SHARE or more below their lowest so far
    drops: tuple[Flight, ...] = ()
    rises: tuple[Flight, ...] = ()
    deals: tuple[Deal, ...] = ()

    @property
    def new_flights(self) -> int:
        return self.flights - self.compared

    @property
    def cheapest_change(self) -> Decimal | None:
        if self.cheapest is None or self.cheapest_before is None:
            return None
        return self.cheapest.price - self.cheapest_before


def moved(price: Decimal, before: Decimal) -> int:
    """-1 / +1 when `price` is at least CHANGE_SHARE below / above `before`, else 0."""
    if price <= before * (1 - CHANGE_SHARE):
        return -1
    if price >= before * (1 + CHANGE_SHARE):
        return 1
    return 0


def search_report(
    currency: str,
    flights: list[Flight],
    previous: "SearchReport | None" = None,
    deals: list[Deal] | tuple[Deal, ...] = (),
) -> SearchReport:
    """`flights`: the real, upcoming prices of this poll in `currency`; `previous`: the report
    of the poll before (for the cheapest prices then)."""
    compared = [(f, f.previous) for f in flights if f.previous is not None]
    moves = [moved(f.price, before) for f, before in compared]
    earlier = previous if previous is not None and previous.currency == currency else None
    if earlier is not None and earlier.cheapest is not None:
        cheapest_before: Decimal | None = earlier.cheapest.price
    else:
        cheapest_before = min((before for _, before in compared), default=None)
    months: dict[date, Decimal] = {}
    for flight in flights:
        month = flight.departure.replace(day=1)
        months[month] = min(months.get(month, flight.price), flight.price)
    before_months = {m.month: m.price for m in earlier.months} if earlier else {}
    if not before_months:
        for flight, before in compared:
            month = flight.departure.replace(day=1)
            before_months[month] = min(before_months.get(month, before), before)
    new_lows = sorted(
        (
            f
            for f, _ in compared
            if f.lowest_before is not None and moved(f.price, f.lowest_before) < 0
        ),
        key=lambda f: (f.price, f.departure),
    )
    # Changed by CHANGE_SHARE or more, biggest drop first (`change` is set for compared ones).
    changed = sorted(
        (f for (f, _), move in zip(compared, moves, strict=True) if move),
        key=lambda f: f.change,
    )
    return SearchReport(
        currency=currency,
        flights=len(flights),
        compared=len(compared),
        cheaper=moves.count(-1),
        dearer=moves.count(1),
        cheapest=min(flights, key=lambda f: (f.price, f.departure), default=None),
        cheapest_before=cheapest_before,
        months=tuple(
            MonthPrice(month, price, before_months.get(month))
            for month, price in sorted(months.items())
        ),
        new_low_count=len(new_lows),
        new_lows=tuple(new_lows[:TOP]),
        drops=tuple(f for f in changed if f.change < 0)[:TOP],
        rises=tuple(f for f in reversed(changed) if f.change > 0)[:TOP],
        deals=tuple(deals),
    )


def notable(report: SearchReport | None) -> bool:
    """Worth telling: an unusually cheap price, the cheapest price moved, a new lowest price, or
    many flights changed. The first poll (nothing to compare) only with a deal."""
    if report is None:
        return False
    if report.deals:
        return True
    if not report.compared:
        return False
    before = report.cheapest_before
    if report.cheapest is not None and before is not None and moved(report.cheapest.price, before):
        return True
    if report.new_low_count:
        return True
    return report.cheaper + report.dearer >= report.compared * MANY_CHANGED_SHARE


# --- JSON (poll_reports.facts) ---------------------------------------------------------------


def amount_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def decimal_or_none(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def flight_facts(flight: Flight) -> dict:
    return {
        "origin": flight.origin,
        "destination": flight.destination,
        "cabin": flight.cabin,
        "departure": flight.departure.isoformat(),
        "return": flight.returning.isoformat() if flight.returning else None,
        "price": str(flight.price),
        "previous": amount_text(flight.previous),
        "lowest_before": amount_text(flight.lowest_before),
    }


def flight_from_facts(data: dict) -> Flight:
    return Flight(
        origin=data["origin"],
        destination=data["destination"],
        cabin=data["cabin"],
        departure=date.fromisoformat(data["departure"]),
        returning=date.fromisoformat(data["return"]) if data.get("return") else None,
        price=Decimal(data["price"]),
        previous=decimal_or_none(data.get("previous")),
        lowest_before=decimal_or_none(data.get("lowest_before")),
    )


def to_facts(report: SearchReport) -> dict:
    return {
        "version": FACTS_VERSION,
        "kind": "search",
        "currency": report.currency,
        "flights": report.flights,
        "compared": report.compared,
        "cheaper": report.cheaper,
        "dearer": report.dearer,
        "cheapest": flight_facts(report.cheapest) if report.cheapest else None,
        "cheapest_before": amount_text(report.cheapest_before),
        "months": [
            {
                "month": m.month.isoformat(),
                "price": str(m.price),
                "previous": amount_text(m.previous),
            }
            for m in report.months
        ],
        "new_low_count": report.new_low_count,
        "new_lows": [flight_facts(f) for f in report.new_lows],
        "drops": [flight_facts(f) for f in report.drops],
        "rises": [flight_facts(f) for f in report.rises],
        "deals": [{"flight": flight_facts(d.flight), "usual": str(d.usual)} for d in report.deals],
    }


def from_facts(data: dict | None) -> SearchReport | None:
    """None for facts of another version (or none)."""
    if not data or data.get("version") != FACTS_VERSION or data.get("kind") != "search":
        return None
    return SearchReport(
        currency=data["currency"],
        flights=data["flights"],
        compared=data["compared"],
        cheaper=data["cheaper"],
        dearer=data["dearer"],
        cheapest=flight_from_facts(data["cheapest"]) if data.get("cheapest") else None,
        cheapest_before=decimal_or_none(data.get("cheapest_before")),
        months=tuple(
            MonthPrice(
                date.fromisoformat(m["month"]),
                Decimal(m["price"]),
                decimal_or_none(m.get("previous")),
            )
            for m in data.get("months", [])
        ),
        new_low_count=data.get("new_low_count", 0),
        new_lows=tuple(flight_from_facts(f) for f in data.get("new_lows", [])),
        drops=tuple(flight_from_facts(f) for f in data.get("drops", [])),
        rises=tuple(flight_from_facts(f) for f in data.get("rises", [])),
        deals=tuple(
            Deal(flight_from_facts(d["flight"]), Decimal(d["usual"])) for d in data.get("deals", [])
        ),
    )
