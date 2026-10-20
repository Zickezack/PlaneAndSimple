"""Trip reports: the cheapest working option against the poll before, and when that is notable."""

from datetime import date
from decimal import Decimal

from flighttracker.domain.trip_report import LegPrice, from_facts, notable, to_facts, trip_report
from flighttracker.services import report_texts


def legs(*prices, previous=None, day=3):
    return [
        LegPrice("ZRH", "BCN", date(2027, 1, day), "10:00", Decimal(prices[0]), previous),
        LegPrice("BCN", "ZRH", date(2027, 1, day + 7), "18:00", Decimal(prices[1])),
    ]


def test_first_report_is_not_notable():
    report = trip_report("CHF", 4, legs("100", "120"), None)
    assert report.total == Decimal("220") and report.options_before is None
    assert not notable(report)
    assert report_texts.trip_lines(report, "en") == [
        "Cheapest option now 220.00 CHF.",
        "Working options: 4.",
    ]


def test_cheaper_other_flights_and_new_lows():
    first = trip_report("CHF", 4, legs("100", "120"), None)
    same = trip_report("CHF", 4, legs("101", "120", previous=Decimal("100")), first)
    assert same.same_option and not notable(same)  # 0.5 % – not notable
    cheaper = trip_report("CHF", 5, legs("80", "120", previous=Decimal("100")), same)
    assert cheaper.change == Decimal("-21") and cheaper.new_low and notable(cheaper)
    assert cheaper.lowest_before == Decimal("220")
    lines = report_texts.trip_lines(cheaper, "en")
    assert lines[0] == "Cheapest option now 200.00 CHF (▼ 21.00 CHF since the poll before)."
    assert "A new lowest price for this Trip." in lines
    assert lines[-1].startswith("Biggest change: leg 1, ZRH → BCN on Sun, 3 Jan 2027, ▼ 20.00 CHF")
    other = trip_report("CHF", 5, legs("80", "120", day=4), cheaper)
    assert not other.same_option and notable(other)  # same price, other flights
    assert report_texts.trip_short(other, "en") == "200.00 CHF · 5 working options"
    assert from_facts(to_facts(other)) == other


def test_options_vanishing_is_notable():
    first = trip_report("CHF", 3, legs("100", "120"), None)
    gone = trip_report("CHF", 0, [], first)
    assert gone.total is None and notable(gone)
    assert report_texts.trip_short(gone, "en") == "No complete option in this poll."
