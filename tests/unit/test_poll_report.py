"""Poll reports: what a poll changed and when it is worth telling; the deal radar's rule."""

from datetime import date
from decimal import Decimal

from flighttracker.domain.deals import MIN_DATES, unusual_prices
from flighttracker.domain.poll_report import (
    Flight,
    from_facts,
    notable,
    search_report,
    to_facts,
)
from flighttracker.services import report_texts


def flight(day: int, price: str, previous: str | None = None, lowest: str | None = None, **kw):
    return Flight(
        kw.get("origin", "ZRH"),
        kw.get("destination", "BCN"),
        "economy",
        date(2027, 1, day),
        date(2027, 1, day + 7) if kw.get("round_trip", True) else None,
        Decimal(price),
        Decimal(previous) if previous else None,
        Decimal(lowest or previous) if (lowest or previous) else None,
    )


def test_the_first_poll_is_never_notable_without_a_deal():
    report = search_report("CHF", [flight(3, "200"), flight(4, "180")])
    assert (report.flights, report.compared, report.cheapest.price) == (2, 0, Decimal("180"))
    assert report.cheapest_before is None and not notable(report)
    assert report_texts.search_lines(report, "en")[-1] == "First poll: 2 flights found."


def test_small_changes_are_not_notable():
    flights = [flight(d, "200", "202") for d in range(1, 9)]  # 1 % cheaper each
    report = search_report("CHF", flights)
    assert (report.cheaper, report.dearer, report.new_low_count) == (0, 0, 0)
    assert not notable(report)
    lines = report_texts.search_lines(report, "en")
    assert lines[0] == "No notable change since the poll before."
    assert "Cheapest flight now 200.00 CHF (▼ 2.00 CHF since the poll before)" in lines[1]


def test_cheapest_new_lows_drops_and_rises():
    flights = [
        flight(3, "150", "200", lowest="190"),  # new low, biggest drop
        flight(4, "210", "200"),  # 5 % dearer
        flight(5, "199", "200"),  # unchanged (below 3 %)
        flight(6, "250"),  # new in this poll
    ]
    previous = search_report("CHF", [flight(3, "200"), flight(4, "200")])
    report = search_report("CHF", flights, previous)
    assert report.cheapest_before == Decimal("200")  # the cheapest of the poll before
    assert (report.compared, report.cheaper, report.dearer, report.new_flights) == (3, 1, 1, 1)
    assert [f.departure.day for f in report.new_lows] == [3]
    assert [f.departure.day for f in report.drops] == [3]
    assert [f.departure.day for f in report.rises] == [4]
    assert notable(report)
    assert report.months[0].price == Decimal("150") and report.months[0].previous == Decimal("200")
    assert from_facts(to_facts(report)) == report  # stored and read back unchanged
    lines = report_texts.search_lines(report, "de")
    assert lines[0].startswith("Günstigster Flug jetzt 150.00 CHF (▼ 50.00 CHF")
    assert report_texts.search_short(report, "en") == "150.00 CHF ▼ 50.00 CHF · 1 new lowest price"


def test_many_changed_flights_are_notable():
    flights = [flight(d, "206", "200") for d in range(1, 5)] + [
        flight(d, "200", "200") for d in range(5, 13)
    ]
    report = search_report("CHF", flights)
    assert report.dearer == 4 and report.compared == 12  # a third changed
    assert notable(report)


def test_deal_radar_needs_enough_dates_and_a_big_gap():
    usual = [flight(d, "200") for d in range(1, MIN_DATES)]
    assert unusual_prices([*usual, flight(20, "90")]) != []
    assert unusual_prices([*usual[:-1], flight(20, "90")]) == []  # too few dates
    assert unusual_prices([*usual, flight(20, "130")]) == []  # 35 % below: not unusual
    (deal,) = unusual_prices([*usual, flight(20, "90")])
    assert deal.usual == Decimal("200.00") and deal.percent_below == 55
    one_way = [flight(d, "200", round_trip=False) for d in range(1, MIN_DATES)]
    assert unusual_prices([*one_way, flight(20, "90")]) == []  # compared with one-ways only
    report = search_report(
        "CHF", [*usual, flight(20, "90")], deals=unusual_prices([*usual, flight(20, "90")])
    )
    assert notable(report)  # even on the first poll
    assert "55 % below the usual 200.00 CHF" in report_texts.search_lines(report, "en")[-1]


def test_facts_of_another_version_are_ignored():
    assert from_facts({"version": 99}) is None and from_facts(None) is None
