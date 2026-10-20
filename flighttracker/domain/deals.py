"""Deal radar: prices far below what the same route usually costs right now – possibly an error
fare. Rule-based and explainable; an AI model may only put it into words.

"Usual" is the median price of the same route and cabin (one-way or round trip) over the
departure dates of the same poll, so the rule needs no history and adapts to seasons.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

# At most this share of the usual price counts as unusually cheap (40 % or more below it).
DEAL_SHARE = Decimal("0.6")
# The median of fewer departure dates says too little about the usual price.
MIN_DATES = 8
# A poll reports at most this many – the cheapest relative to their usual price first.
MAX_DEALS = 5


class Priced(Protocol):
    @property
    def origin(self) -> str: ...
    @property
    def destination(self) -> str: ...
    @property
    def cabin(self) -> str: ...
    @property
    def departure(self) -> date: ...
    @property
    def returning(self) -> date | None: ...
    @property
    def price(self) -> Decimal: ...


@dataclass(frozen=True)
class Deal:
    flight: Priced
    usual: Decimal  # median price of its route and cabin in the same poll

    @property
    def percent_below(self) -> int:
        return int((1 - self.flight.price / self.usual) * 100) if self.usual else 0


def unusual_prices(flights: Iterable[Priced]) -> list[Deal]:
    groups: dict[tuple, list[Priced]] = defaultdict(list)
    for flight in flights:
        key = (flight.origin, flight.destination, flight.cabin, flight.returning is None)
        groups[key].append(flight)
    deals = []
    for group in groups.values():
        if len({flight.departure for flight in group}) < MIN_DATES:
            continue
        usual = Decimal(statistics.median(flight.price for flight in group))
        usual = usual.quantize(Decimal("0.01"))
        deals += [Deal(flight, usual) for flight in group if flight.price <= usual * DEAL_SHARE]
    deals.sort(key=lambda deal: (deal.flight.price / deal.usual, deal.flight.departure))
    return deals[:MAX_DEALS]
