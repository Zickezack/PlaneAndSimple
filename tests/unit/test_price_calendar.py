"""Price calendar (heat map): Google's GetCalendarGraph request/response and the chunking."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import unquote

import pytest

from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.domain.locations import LocationRef
from flighttracker.domain.spec import SearchSpec, calendar_requests_per_poll, requests_per_poll
from flighttracker.providers.base import CalendarQuery, ProviderError, calendar_chunks
from flighttracker.providers.google_flights import (
    GoogleFlightsProvider,
    calendar_request_body,
    parse_calendar,
)
from flighttracker.providers.mock import MockProvider

NOW = datetime(2026, 10, 10, 6, tzinfo=UTC)


def query(**overrides) -> CalendarQuery:
    values = {
        "origin": "ZRH",
        "destination": "BKK",
        "cabin_class": CabinClass.ECONOMY,
        "first_day": date(2026, 10, 11),
        "last_day": date(2027, 1, 31),
        "stay_days": 14,
        "max_stops": None,
        "adults": 2,
        "currency": "CHF",
    }
    return CalendarQuery(**(values | overrides))


def response(days: list[list], *, length_prefixed: bool = False) -> str:
    """A GetCalendarGraph answer in Google's shape (meta data first, the days last)."""
    inner = json.dumps([[None, [[1, 2, 3]], 0, "token"], days])
    chunk = json.dumps([["wrb.fr", None, inner], ["di", 42]])
    if length_prefixed:
        return f")]}}'\n\n{len(chunk.encode()) + 2}\n{chunk}\n"
    return f")]}}'\n\n{chunk}"


def decoded(body: str) -> list:
    return json.loads(json.loads(unquote(body.removeprefix("f.req=")))[1])


class TestRequest:
    def test_round_trip_asks_for_the_date_range_and_stay(self):
        filters = decoded(calendar_request_body(query(), date(2026, 10, 11), date(2026, 12, 10)))
        settings = filters[1]
        assert (settings[2], settings[5], settings[6]) == (1, 1, [2, 0, 0, 0])
        outbound, back = settings[13]
        assert (outbound[0], outbound[1], outbound[6]) == (
            [[["ZRH", 0]]],
            [[["BKK", 0]]],
            "2026-10-11",
        )
        assert (back[0], back[6]) == ([[["BKK", 0]]], "2026-10-25")
        assert filters[2:] == [["2026-10-11", "2026-12-10"], None, [14, 14]]

    @pytest.mark.parametrize(("max_stops", "code"), [(None, 0), (0, 1), (1, 2), (2, 3), (3, 0)])
    def test_one_way_with_stop_limit_and_cabin(self, max_stops, code):
        q = query(stay_days=None, max_stops=max_stops, cabin_class=CabinClass.BUSINESS)
        filters = decoded(calendar_request_body(q, date(2026, 11, 1), date(2026, 11, 30)))
        settings = filters[1]
        assert (settings[2], settings[5], len(settings[13])) == (2, 3, 1)
        assert settings[13][0][3] == code
        assert filters[2:] == [["2026-11-01", "2026-11-30"]]


class TestResponse:
    @pytest.mark.parametrize("length_prefixed", [False, True])
    def test_cheapest_price_per_day(self, length_prefixed):
        text = response(
            [
                ["2026-11-01", "2026-11-15", [[None, 1402], "x"], 1],
                ["2026-11-02", "2026-11-16", [[None, 1320], "x"], 1],
                ["2026-11-03", "2026-11-17", [], 1],  # no price that day
            ],
            length_prefixed=length_prefixed,
        )
        prices = parse_calendar(text, "CHF")
        assert [(p.departure_date, p.return_date, p.price) for p in prices] == [
            (date(2026, 11, 1), date(2026, 11, 15), Decimal("1402")),
            (date(2026, 11, 2), date(2026, 11, 16), Decimal("1320")),
        ]

    def test_one_way_days_have_no_return(self):
        (only,) = parse_calendar(response([["2026-11-01", None, [[None, 99], "x"], 1]]), "EUR")
        assert (only.return_date, only.currency) == (None, "EUR")

    def test_other_pages_are_rejected(self):
        with pytest.raises(ValueError):
            parse_calendar("<html>Before you continue</html>", "CHF")


class TestProvider:
    def test_splits_into_61_day_requests_from_tomorrow(self):
        bodies = []

        def search(body, currency):
            bodies.append(decoded(body)[2])
            return response([["2026-10-12", "2026-10-26", [[None, 500], "x"], 1]])

        provider = GoogleFlightsProvider(
            request_delay_seconds=0, calendar_search=search, clock=lambda: NOW, sleep=lambda s: None
        )
        prices = provider.fetch_calendar(query(first_day=date(2026, 10, 1)))
        assert bodies == [["2026-10-11", "2026-12-10"], ["2026-12-11", "2027-01-31"]]
        # Days outside a request's own range (here: in the second request) are ignored.
        assert [p.departure_date for p in prices] == [date(2026, 10, 12)]

    def test_a_failed_chunk_is_skipped_all_failing_raises(self):
        answers = iter(
            [ProviderError("timeout"), response([["2026-12-20", None, [[None, 7], "x"], 1]])]
        )

        def search(body, currency):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        provider = GoogleFlightsProvider(
            calendar_search=search, clock=lambda: NOW, sleep=lambda s: None
        )
        assert [p.price for p in provider.fetch_calendar(query(stay_days=None))] == [Decimal("7")]

        def broken(body, currency):
            return "<title>Before you continue</title>"

        provider = GoogleFlightsProvider(
            calendar_search=broken, clock=lambda: NOW, sleep=lambda s: None
        )
        with pytest.raises(ProviderError, match="consent"):
            provider.fetch_calendar(query())

    def test_mock_has_a_price_for_every_day(self):
        prices = MockProvider(clock=lambda: NOW).fetch_calendar(
            query(first_day=date(2026, 10, 1), last_day=date(2026, 10, 20))
        )
        assert [p.departure_date.day for p in prices] == list(range(11, 21))
        assert all(p.return_date == date(2026, 10, p.departure_date.day + 14) for p in prices[:3])


def test_calendar_chunks():
    assert calendar_chunks(date(2026, 1, 1), date(2026, 1, 3), 2) == [
        (date(2026, 1, 1), date(2026, 1, 2)),
        (date(2026, 1, 3), date(2026, 1, 3)),
    ]
    assert calendar_chunks(date(2026, 1, 5), date(2026, 1, 4), 61) == []


def test_calendar_requests_count_towards_the_poll():
    spec = SearchSpec(
        frozenset({LocationRef.airport("ZRH")}),
        frozenset({LocationRef.airport("BKK"), LocationRef.airport("HKT")}),
        SearchFilters(months_ahead=6, stay_days_min=14, stay_days_max=15, days_per_month=4),
    )
    # 2 routes × 2 stays × 4 requests (6 months of at most 31 days, 61 days per request).
    assert calendar_requests_per_poll(spec, 61) == 16
    assert requests_per_poll(spec, samples_days=True, calendar_days=61) == 2 * 6 * 4 * 2 + 16
    assert requests_per_poll(spec, samples_days=True) == 96
