"""'When to book': a rule-based reading of one flight's observed prices.

Deterministic and explainable – every signal names the numbers behind it. An AI model may later
put the same facts into sentences (services/ai_prompts.py), but never changes them.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# A change of at least this share between two polls counts as rising or falling.
TREND_SHARE = Decimal("0.03")
# The current price is above / below average by more than this share.
AVERAGE_SHARE = Decimal("0.05")
# Closer to departure fares tend to rise; waiting rarely pays off.
SOON_DAYS = 21
# Far from departure a falling price may still fall further.
FAR_DAYS = 42

LOWEST = "lowest"
BELOW_AVERAGE = "below_average"
ABOVE_AVERAGE = "above_average"
RISING = "rising"
FALLING = "falling"
SOON = "soon"

BOOK = "book"  # a good moment
WAIT = "wait"  # likely to get cheaper
WATCH = "watch"  # nothing clear yet


@dataclass(frozen=True)
class BookingAdvice:
    verdict: str
    signals: tuple[str, ...]
    current: Decimal
    first: Decimal
    lowest: Decimal
    highest: Decimal
    average: Decimal
    observations: int
    days_to_departure: int


def advise(prices: list[Decimal], departure: date, today: date) -> BookingAdvice | None:
    """`prices`: every observed price of the flight, oldest first, the current one last."""
    if not prices:
        return None
    current, previous = prices[-1], prices[-2] if len(prices) > 1 else None
    lowest, highest = min(prices), max(prices)
    average = sum(prices) / len(prices)
    days = (departure - today).days
    signals = []
    if len(prices) > 1:
        if current <= lowest and highest > lowest:
            signals.append(LOWEST)
        if current < average * (1 - AVERAGE_SHARE):
            signals.append(BELOW_AVERAGE)
        elif current > average * (1 + AVERAGE_SHARE):
            signals.append(ABOVE_AVERAGE)
        if previous and current >= previous * (1 + TREND_SHARE):
            signals.append(RISING)
        elif previous and current <= previous * (1 - TREND_SHARE):
            signals.append(FALLING)
    if 0 <= days <= SOON_DAYS:
        signals.append(SOON)
    return BookingAdvice(
        verdict=_verdict(set(signals), days),
        signals=tuple(signals),
        current=current,
        first=prices[0],
        lowest=lowest,
        highest=highest,
        average=average.quantize(Decimal("0.01")),
        observations=len(prices),
        days_to_departure=days,
    )


def _verdict(signals: set[str], days: int) -> str:
    if LOWEST in signals or (BELOW_AVERAGE in signals and RISING in signals):
        return BOOK
    if SOON in signals and FALLING not in signals:
        return BOOK
    if FALLING in signals and (ABOVE_AVERAGE in signals or days > FAR_DAYS):
        return WAIT
    if ABOVE_AVERAGE in signals and days > FAR_DAYS:
        return WAIT
    return WATCH
