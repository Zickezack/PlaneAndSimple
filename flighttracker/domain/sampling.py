"""Which departure days a poll asks for (pure logic, no provider or DB)."""

import calendar
from collections.abc import Mapping
from datetime import date
from decimal import Decimal


def sample_days(month: date, count: int, after: date | None = None) -> list[date]:
    """`count` evenly spread departure days within the month, all later than `after`.

    In the current month only the remaining days are sampled, so no request is wasted.
    """
    days_in_month = calendar.monthrange(month.year, month.month)[1]
    first = 1
    if after is not None and (after.year, after.month) == (month.year, month.month):
        first = after.day + 1
    elif after is not None and after >= month.replace(day=days_in_month):
        return []
    available = days_in_month - first + 1
    if available <= 0:
        return []
    step = available / min(count, available)
    return sorted({month.replace(day=first + int(i * step)) for i in range(min(count, available))})


def choose_days(month: date, count: int, after: date, prices: Mapping[date, Decimal]) -> list[date]:
    """`count` departure days of the month, all later than `after`.

    Half of them are evenly spread and stay the same from poll to poll (a continuous price
    history and previous-year comparison), the other half are the cheapest days of the price
    calendar (`prices`: cheapest price per departure day). Without calendar prices the days
    are filled up evenly spread, so a missing calendar never costs a day.
    """
    spread = sample_days(month, count, after)
    if len(spread) < count:
        return spread  # every remaining day of the month
    fixed = sample_days(month, count // 2, after) if count >= 2 else []
    candidates = sorted(
        (price, day)
        for day, price in prices.items()
        if (day.year, day.month) == (month.year, month.month) and day > after and day not in fixed
    )
    chosen = set(fixed) | {day for _, day in candidates[: count - len(fixed)]}
    for day in spread + sample_days(month, 31, after):
        if len(chosen) >= count:
            break
        chosen.add(day)
    return sorted(chosen)
