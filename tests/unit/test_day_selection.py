"""Calendar-driven departure days: half fixed, half the cheapest of the price calendar."""

from datetime import date
from decimal import Decimal

from flighttracker.domain.sampling import choose_days, sample_days

NOV = date(2026, 11, 1)
BEFORE = date(2026, 10, 12)


def prices(**by_day) -> dict[date, Decimal]:
    return {NOV.replace(day=int(day[1:])): Decimal(price) for day, price in by_day.items()}


def test_half_fixed_half_cheapest():
    calendar = prices(d1=50, d5=90, d9=300, d20=95, d28=400)
    # Fixed: 1 and 16 (evenly spread, a subset of the old 1, 8, 16, 23); cheapest: 5 and 20 –
    # the 1st is cheapest too, but it is already a fixed day.
    assert sample_days(NOV, 2) == [date(2026, 11, 1), date(2026, 11, 16)]
    assert choose_days(NOV, 4, BEFORE, calendar) == [date(2026, 11, d) for d in (1, 5, 16, 20)]


def test_without_calendar_the_days_stay_evenly_spread():
    assert choose_days(NOV, 4, BEFORE, {}) == sample_days(NOV, 4)
    # One priced day: the missing one is filled up evenly spread.
    assert choose_days(NOV, 4, BEFORE, prices(d12=80)) == [
        date(2026, 11, d) for d in (1, 8, 12, 16)
    ]


def test_single_day_is_the_cheapest_and_every_day_stays_every_day():
    assert choose_days(NOV, 1, BEFORE, prices(d3=200, d17=120)) == [date(2026, 11, 17)]
    assert choose_days(NOV, 31, BEFORE, prices(d3=200)) == sample_days(NOV, 31)


def test_only_upcoming_days_of_the_month_count():
    calendar = {date(2026, 11, 5): Decimal(10), date(2026, 12, 2): Decimal(1)}
    days = choose_days(NOV, 4, date(2026, 11, 10), calendar)
    assert all(day > date(2026, 11, 10) and day.month == 11 for day in days)
    assert len(days) == 4
