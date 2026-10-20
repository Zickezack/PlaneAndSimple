"""What a completed Trip poll changed: how many working options there are and what the cheapest
one costs, compared with the poll before – and whether that is worth telling ("notable").

Pure and deterministic like the tracked search's report (domain/poll_report.py); stored as
JSON in `poll_reports.facts` of the Trip.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from flighttracker.domain.poll_report import amount_text, decimal_or_none, moved

FACTS_VERSION = 1


@dataclass(frozen=True)
class LegPrice:
    origin: str
    destination: str
    departure: date
    departure_time: str | None
    price: Decimal
    previous: Decimal | None = None  # the same flight at the poll before

    @property
    def key(self) -> tuple[str, str, date, str | None]:
        return (self.origin, self.destination, self.departure, self.departure_time)

    @property
    def change(self) -> Decimal | None:
        return None if self.previous is None else self.price - self.previous


@dataclass(frozen=True)
class TripReport:
    currency: str
    options: int  # working options (at most the planner's limit)
    options_before: int | None  # None: no report of a poll before
    total: Decimal | None  # the cheapest option now
    total_before: Decimal | None  # the cheapest option at the poll before
    lowest_before: Decimal | None  # the lowest cheapest option of all earlier reports
    legs: tuple[LegPrice, ...] = ()  # of the cheapest option
    same_option: bool = True  # the cheapest option has the same flights as before

    @property
    def change(self) -> Decimal | None:
        if self.total is None or self.total_before is None:
            return None
        return self.total - self.total_before

    @property
    def lowest(self) -> Decimal | None:
        values = [v for v in (self.total, self.lowest_before) if v is not None]
        return min(values) if values else None

    @property
    def new_low(self) -> bool:
        """The cheapest option is 3 % or more below every earlier one."""
        return (
            self.total is not None
            and self.lowest_before is not None
            and moved(self.total, self.lowest_before) < 0
        )

    @property
    def biggest_leg_change(self) -> tuple[int, LegPrice] | None:
        """(position from 1, leg) of the cheapest option's leg whose price changed most."""
        changed = [(i, leg) for i, leg in enumerate(self.legs, 1) if leg.change]
        return max(changed, key=lambda item: abs(item[1].change), default=None)


def trip_report(
    currency: str,
    options: int,
    legs: list[LegPrice],
    previous: "TripReport | None",
) -> TripReport:
    """`legs`: the cheapest working option (none: no option); `previous`: the report of the
    poll before."""
    total = sum((leg.price for leg in legs), Decimal(0)) if legs else None
    earlier = previous if previous is not None and previous.currency == currency else None
    if earlier is None:
        return TripReport(currency, options, None, total, None, None, tuple(legs))
    return TripReport(
        currency=currency,
        options=options,
        options_before=earlier.options,
        total=total,
        total_before=earlier.total,
        lowest_before=earlier.lowest,
        legs=tuple(legs),
        same_option=[leg.key for leg in legs] == [leg.key for leg in earlier.legs],
    )


def notable(report: TripReport | None) -> bool:
    """Worth telling: options appeared or vanished, the cheapest one moved by 3 % or more, has
    other flights now, or is a new lowest price. Never the first report."""
    if report is None or report.options_before is None:
        return False
    if (report.options > 0) != (report.options_before > 0):
        return True
    if report.total is None or report.total_before is None:
        return False
    return (
        bool(moved(report.total, report.total_before)) or not report.same_option or report.new_low
    )


def to_facts(report: TripReport) -> dict:
    return {
        "version": FACTS_VERSION,
        "kind": "trip",
        "currency": report.currency,
        "options": report.options,
        "options_before": report.options_before,
        "total": amount_text(report.total),
        "total_before": amount_text(report.total_before),
        "lowest_before": amount_text(report.lowest_before),
        "same_option": report.same_option,
        "legs": [
            {
                "origin": leg.origin,
                "destination": leg.destination,
                "departure": leg.departure.isoformat(),
                "departure_time": leg.departure_time,
                "price": str(leg.price),
                "previous": amount_text(leg.previous),
            }
            for leg in report.legs
        ],
    }


def from_facts(data: dict | None) -> TripReport | None:
    if not data or data.get("version") != FACTS_VERSION or data.get("kind") != "trip":
        return None
    return TripReport(
        currency=data["currency"],
        options=data["options"],
        options_before=data.get("options_before"),
        total=decimal_or_none(data.get("total")),
        total_before=decimal_or_none(data.get("total_before")),
        lowest_before=decimal_or_none(data.get("lowest_before")),
        legs=tuple(
            LegPrice(
                leg["origin"],
                leg["destination"],
                date.fromisoformat(leg["departure"]),
                leg.get("departure_time"),
                Decimal(leg["price"]),
                decimal_or_none(leg.get("previous")),
            )
            for leg in data.get("legs", [])
        ),
        same_option=data.get("same_option", True),
    )
