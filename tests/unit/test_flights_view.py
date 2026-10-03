from datetime import UTC, date, datetime
from decimal import Decimal

from flighttracker.services.history import PricePoint
from flighttracker.web.flights import (
    chart_data,
    flightradar_url,
    format_duration,
    segments,
    summarize,
)
from flighttracker.web.routes.searches import _script_json

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
DETAILS = {
    "airlines": ["Qatar Airways"],
    "departure_time": "15:10",
    "arrival_date": "2026-11-14",
    "arrival_time": "18:50",
    "duration_min": 2740,
    "segments": [
        {
            "flight": "QR 96",
            "to": "DOH",
            "arrival_date": "2026-11-12",
            "arrival_time": "22:50",
        },
        {
            "flight": "QR 832",
            "departure_date": "2026-11-14",
            "departure_time": "08:20",
        },
    ],
    "url": "https://www.google.com/travel/flights?tfs=abc",
}


def point(**overrides) -> PricePoint:
    values = {
        "origin": "ZRH",
        "destination": "BKK",
        "cabin_class": "economy",
        "currency": "CHF",
        "departure_date": date(2026, 11, 12),
        "return_date": date(2026, 11, 26),
        "stops": 1,
        "price": Decimal("516"),
        "previous_price": Decimal("530"),
        "airline": "Qatar Airways",
        "details": DETAILS,
        "observed_at": NOW,
        "provider": "google_flights",
        "fake": False,
    }
    return PricePoint(**(values | overrides))


def test_summary_with_layover_and_arrival_two_days_later():
    summary = summarize(point())
    assert summary.flights == ["QR 96", "QR 832"]
    assert summary.arrival_day_offset == 2
    assert [(lay.airport, format_duration(lay.minutes)) for lay in summary.layovers] == [
        ("DOH", "33h 30m")
    ]
    assert summary.url.startswith("https://www.google.com/travel/flights")


def test_no_details_no_summary():
    assert summarize(point(details=None)) is None
    assert format_duration(None) == "–"


def test_point_change_and_stay():
    p = point()
    assert (p.change, p.stay_days, p.month) == (Decimal("-14"), 14, date(2026, 11, 1))


def test_chart_data_upcoming_in_search_currency_with_previous_year():
    last_year = point(
        departure_date=date(2025, 11, 12), return_date=date(2025, 11, 26), price=Decimal("480")
    )
    past = point(departure_date=date(2026, 9, 1))
    other_currency = point(currency="EUR", departure_date=date(2026, 11, 13))
    data = chart_data([last_year, past, point(), other_currency], date(2026, 9, 29), "CHF", {})
    (only,) = data["points"]
    assert (only["date"], only["price"], only["py"], only["prev"]) == (
        "2026-11-12",
        516.0,
        480.0,
        530.0,
    )
    assert only["via"] == [{"airport": "DOH", "wait": "33h 30m"}]
    assert data["routes"] == ["ZRH-BKK"]


def test_script_json_cannot_close_the_script_tag():
    encoded = _script_json({"name": "</script><script>alert(1)</script> & more"})
    assert "</" not in encoded and "<" not in encoded and "&" not in encoded


def test_flightradar_links():
    assert flightradar_url("QR 96") == "https://www.flightradar24.com/data/flights/qr96"
    assert flightradar_url(None) is None
    assert flightradar_url("QR/96") is None


def test_segments_with_layover():
    first, second = segments(DETAILS)
    assert (first.flight, first.flightradar, first.layover) == (
        "QR 96",
        "https://www.flightradar24.com/data/flights/qr96",
        "33h 30m",
    )
    assert second.layover is None
    assert segments(None) == []


def test_sparkline_direction_and_flat_series():
    from decimal import Decimal

    from flighttracker.web.flights import sparkline

    assert sparkline([Decimal(1)]) == ""
    assert "spark-down" in sparkline([Decimal(10), Decimal(8)])
    assert "spark-up" in sparkline([Decimal(8), Decimal(10), Decimal(12)])
    assert "spark-flat" in sparkline([Decimal(5), Decimal(5)])
