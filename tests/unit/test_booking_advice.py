from datetime import date, timedelta
from decimal import Decimal

from flighttracker.domain.booking_advice import BOOK, WAIT, WATCH, advise

TODAY = date(2026, 10, 14)
FAR = TODAY + timedelta(days=90)


def prices(*values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def test_lowest_price_seen_is_a_good_moment():
    advice = advise(prices(300, 280, 260), FAR, TODAY)
    assert advice.verdict == BOOK
    assert advice.signals == ("lowest", "below_average", "falling")
    assert (advice.first, advice.lowest, advice.highest) == (300, 260, 300)
    assert advice.average == Decimal("280.00") and advice.observations == 3


def test_falling_far_from_departure_says_wait():
    advice = advise(prices(250, 340, 320), FAR, TODAY)
    assert advice.verdict == WAIT
    assert "falling" in advice.signals and "above_average" in advice.signals


def test_close_to_departure_book_unless_falling():
    soon = TODAY + timedelta(days=10)
    assert advise(prices(300, 305), soon, TODAY).verdict == BOOK
    falling = advise(prices(280, 320, 300), soon, TODAY)
    assert falling.signals == ("falling", "soon") and falling.verdict == WATCH
    # After a drop to the lowest price seen it is a good moment again.
    assert advise(prices(320, 300), soon, TODAY).verdict == BOOK


def test_single_observation_has_no_trend():
    advice = advise(prices(199), FAR, TODAY)
    assert advice.signals == () and advice.verdict == WATCH
    assert advise([], FAR, TODAY) is None
