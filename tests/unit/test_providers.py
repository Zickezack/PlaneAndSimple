import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from flighttracker.domain.filters import CabinClass, TripType
from flighttracker.providers.base import PriceQuery, ProviderError
from flighttracker.providers.google_flights import (
    FareOption,
    FareRequest,
    GoogleFlightsProvider,
    fast_flights_search,
    parse_fares,
    sample_days,
)
from flighttracker.providers.mock import MockProvider
from flighttracker.providers.registry import (
    FAKE_DATA_PROVIDERS,
    get_provider,
    provider_class,
    provider_config,
)
from flighttracker.providers.travelpayouts import TravelpayoutsProvider

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def query(**overrides) -> PriceQuery:
    values = {
        "origin": "ZRH",
        "destination": "BCN",
        "departure_month": date(2026, 11, 1),
        "cabin_class": CabinClass.ECONOMY,
        "trip_type": TripType.ROUND_TRIP,
        "max_stops": None,
        "stay_days_min": None,
        "stay_days_max": None,
        "adults": 1,
        "currency": "CHF",
    }
    return PriceQuery(**(values | overrides))


class TestMock:
    def test_deterministic(self):
        provider = MockProvider(clock=lambda: NOW)
        assert provider.fetch_current(query()) == provider.fetch_current(query())

    def test_current_prices_in_month_and_future(self):
        quotes = MockProvider(clock=lambda: NOW).fetch_current(query())
        assert quotes
        for q in quotes:
            assert q.departure_date.month == 11
            assert q.return_date is not None
            assert q.observed_at == NOW

    def test_direct_only(self):
        quotes = MockProvider(clock=lambda: NOW).fetch_current(query(max_stops=0))
        assert {q.stops for q in quotes} == {0}

    def test_one_way_has_no_return(self):
        quotes = MockProvider(clock=lambda: NOW).fetch_current(query(trip_type=TripType.ONE_WAY))
        assert all(q.return_date is None for q in quotes)


def travelpayouts_with(handler) -> TravelpayoutsProvider:
    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return TravelpayoutsProvider("secret-token", client=client, sleep=lambda _: None)


MATRIX_ENTRY = {
    "trip_class": 0,
    "origin": "ZRH",
    "destination": "BCN",
    "depart_date": "2026-11-03",
    "return_date": "2026-11-10",
    "number_of_changes": 0,
    "value": 129,
    "found_at": "2026-09-27T08:15:00+02:00",
}


class TestTravelpayouts:
    def test_maps_month_matrix_and_sends_token_as_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["token"] = request.headers.get("X-Access-Token")
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"success": True, "data": [MATRIX_ENTRY]})

        quotes = travelpayouts_with(handler).fetch_current(query())
        assert seen["token"] == "secret-token"
        assert "token" not in seen["params"]
        assert seen["params"]["month"] == "2026-11-01"
        assert seen["params"]["one_way"] == "false"
        assert len(quotes) == 1
        q = quotes[0]
        assert (q.price, q.stops, q.departure_date) == (Decimal("129"), 0, date(2026, 11, 3))
        assert q.observed_at.utcoffset() is not None
        assert q.provider == "travelpayouts"

    @pytest.mark.parametrize(
        ("entry_overrides", "query_overrides"),
        [
            ({"trip_class": 1}, {}),  # business fare, economy asked
            ({"number_of_changes": 2}, {"max_stops": 1}),
            ({}, {"stay_days_min": 10}),
            ({"value": "n/a"}, {}),
        ],
    )
    def test_filters_non_matching_entries(self, entry_overrides, query_overrides):
        entry = MATRIX_ENTRY | entry_overrides

        def handler(request):
            return httpx.Response(200, json={"success": True, "data": [entry]})

        assert travelpayouts_with(handler).fetch_current(query(**query_overrides)) == []

    def test_http_error_does_not_leak_token(self):
        provider = travelpayouts_with(lambda request: httpx.Response(401, json={}))
        with pytest.raises(ProviderError) as info:
            provider.fetch_current(query())
        assert "secret-token" not in str(info.value)

    @pytest.mark.parametrize("body", [b"<html>maintenance</html>", b"[1, 2]"])
    def test_unexpected_body_fails_only_this_query(self, body):
        provider = travelpayouts_with(lambda request: httpx.Response(200, content=body))
        with pytest.raises(ProviderError):
            provider.fetch_current(query())

    def test_requires_token(self):
        with pytest.raises(ValueError):
            TravelpayoutsProvider("")


class TestGoogleFlights:
    def test_sample_days_spread(self):
        assert sample_days(date(2026, 11, 1), 4) == [
            date(2026, 11, 1),
            date(2026, 11, 8),
            date(2026, 11, 16),
            date(2026, 11, 23),
        ]

    def test_sample_days_only_remaining_days_of_current_month(self):
        assert sample_days(date(2026, 9, 1), 4, after=date(2026, 9, 20)) == [
            date(2026, 9, 21),
            date(2026, 9, 23),
            date(2026, 9, 26),
            date(2026, 9, 28),
        ]
        assert sample_days(date(2026, 9, 1), 4, after=date(2026, 9, 29)) == [date(2026, 9, 30)]
        assert sample_days(date(2026, 9, 1), 4, after=date(2026, 9, 30)) == []
        assert sample_days(date(2026, 8, 1), 4, after=date(2026, 9, 5)) == []

    def test_takes_cheapest_allowed_option_per_day(self):
        requests: list[FareRequest] = []

        def search(request: FareRequest) -> list[FareOption]:
            requests.append(request)
            return [FareOption(300, 0, "LX"), FareOption(150, 2, "XX"), FareOption(200, 1, "IB")]

        provider = GoogleFlightsProvider(search=search, clock=lambda: NOW, sleep=lambda _: None)
        quotes = provider.fetch_current(
            query(max_stops=1, cabin_class=CabinClass.BUSINESS, days_per_month=2)
        )
        assert len(quotes) == 2
        assert {(q.price, q.stops, q.airline) for q in quotes} == {(Decimal(200), 1, "IB")}
        assert requests[0].seat == "business"
        assert requests[0].children == 0
        assert requests[0].return_date is not None

    def test_consent_page_is_reported(self, monkeypatch):
        import primp

        class ConsentClient:
            def __init__(self, **kwargs):
                pass

            def get(self, url, params=None, headers=None):
                assert "SOCS=" in headers["Cookie"]
                return type("Response", (), {"text": "<title>Before you continue</title>"})()

        monkeypatch.setattr(primp, "Client", ConsentClient)
        request = FareRequest("ZRH", "BCN", date(2026, 11, 3), None, "economy", 1, "CHF", None)
        with pytest.raises(ProviderError, match="cookie consent"):
            fast_flights_search(request)

    def test_passes_children(self):
        requests: list[FareRequest] = []

        def search(request: FareRequest) -> list[FareOption]:
            requests.append(request)
            return []

        provider = GoogleFlightsProvider(search=search, clock=lambda: NOW, sleep=lambda _: None)
        provider.fetch_current(query(adults=2, children=1, days_per_month=1))
        assert (requests[0].adults, requests[0].children) == (2, 1)

    def test_queries_every_day_and_stay_length(self):
        requests: list[FareRequest] = []

        def search(request):
            requests.append(request)
            return [FareOption(100 + (request.return_date - request.departure).days, 0, "LX")]

        provider = GoogleFlightsProvider(search=search, clock=lambda: NOW, sleep=lambda _: None)
        quotes = provider.fetch_current(query(days_per_month=2, stay_days_min=7, stay_days_max=9))
        assert len(requests) == 2 * 3
        assert sorted({(q.return_date - q.departure_date).days for q in quotes}) == [7, 8, 9]

    def test_single_failed_day_is_skipped(self):
        calls = iter([ProviderError("boom"), [FareOption(120, 0, "LX")]])

        def search(request):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        provider = GoogleFlightsProvider(search=search, clock=lambda: NOW, sleep=lambda _: None)
        assert [q.price for q in provider.fetch_current(query(days_per_month=2))] == [Decimal(120)]

    def test_fails_when_every_day_fails(self):
        def search(request):
            raise ProviderError("down")

        provider = GoogleFlightsProvider(search=search, clock=lambda: NOW, sleep=lambda _: None)
        with pytest.raises(ProviderError, match="down"):
            provider.fetch_current(query(days_per_month=2))

    @staticmethod
    def page(best, other) -> str:
        data = json.dumps([[1], [2], best, other, None])
        return f'<script class="ds:1" nonce="x">AF_initDataCallback({{data:{data}, sideChannel: {{}}}});</script>'

    def test_parses_best_and_other_flights_and_skips_unpriced(self):
        direct = [["multi", ["THAI"], [["seg"]]], [[None, 946]]]
        one_stop = [["multi", ["Qatar Airways"], [["seg"], ["seg"]]], [[None, 516]]]
        unpriced = [["multi", ["Emirates"], [["seg"], ["seg"]]], [[None]]]
        fares = parse_fares(self.page([[direct]], [[unpriced, one_stop]]))
        assert [(f.price, f.stops, f.airline) for f in fares] == [
            (946, 0, "THAI"),
            (516, 1, "Qatar Airways"),
        ]

    @staticmethod
    def segment(origin, destination, number, dep, arr, day, minutes):
        values = [None] * 23
        values[3], values[6] = origin, destination
        values[8], values[10], values[11] = dep, arr, minutes
        values[17] = "Airbus A350"
        values[20] = values[21] = [2026, 11, day]
        values[22] = ["QR", number, None, "Qatar Airways"]
        return values

    def test_extracts_flight_details(self):
        itinerary = [None] * 10
        itinerary[1] = ["Qatar Airways"]
        itinerary[2] = [
            self.segment("ZRH", "DOH", "96", [15, 10], [22, 50], 12, 340),
            self.segment("DOH", "BKK", "832", [8, 20], [18, 50], 14, 390),
        ]
        itinerary[5], itinerary[7], itinerary[8], itinerary[9] = (
            [15, 10],
            [2026, 11, 14],
            [18, 50],
            2740,
        )
        (fare,) = parse_fares(self.page(None, [[[itinerary, [[None, 516]]]]]))
        details = fare.details
        assert (details["departure_time"], details["arrival_time"]) == ("15:10", "18:50")
        assert (details["arrival_date"], details["duration_min"]) == ("2026-11-14", 2740)
        first, second = details["segments"]
        assert (first["flight"], first["from"], first["to"]) == ("QR 96", "ZRH", "DOH")
        assert (second["departure_date"], second["departure_time"]) == ("2026-11-14", "08:20")
        assert second["aircraft"] == "Airbus A350"

    def test_empty_result_lists(self):
        assert parse_fares(self.page(None, None)) == []
        assert parse_fares(self.page(None, [None])) == []
        no_flights = (
            '<script class="ds:1">AF_initDataCallback({key: "ds:1", data:null, '
            "errorHasStatus: true, sideChannel: {}});</script>"
        )
        assert parse_fares(no_flights) == []

    def test_unexpected_page_raises(self):
        with pytest.raises(ValueError):
            parse_fares("<html>no script</html>")

    def test_skips_past_days(self):
        provider = GoogleFlightsProvider(
            search=lambda r: [FareOption(100, 0, None)], clock=lambda: NOW, sleep=lambda _: None
        )
        quotes = provider.fetch_current(query(departure_month=date(2026, 9, 1)))
        assert all(q.departure_date > NOW.date() for q in quotes)


class _Settings:
    def __init__(self, provider):
        self.flight_provider = provider
        self.travelpayouts_token = None
        self.scraper_days_per_month = 4
        self.scraper_request_delay_seconds = 0


def test_registry_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Available"):
        get_provider(_Settings("nope"))


def test_registry_builds_mock():
    assert get_provider(_Settings("mock")).name == "mock"


def test_capabilities_and_fake_data():
    assert FAKE_DATA_PROVIDERS == {"mock"}
    assert provider_class("google_flights").supports_children
    assert not provider_class("travelpayouts").supports_children
    assert not provider_class("travelpayouts").prices_all_passengers
    with pytest.raises(ValueError, match="Available"):
        provider_class("nope")


def test_mock_prices_scale_with_passengers():
    provider = MockProvider(clock=lambda: NOW)
    single = provider.fetch_current(query())[0].price
    family = provider.fetch_current(query(adults=2, children=2))[0].price
    assert family > single * 3


def test_worker_keeps_its_provider_when_new_settings_are_unusable():
    from flighttracker.worker.__main__ import refresh_provider

    current = get_provider(_Settings("mock"))
    broken = _Settings("travelpayouts")  # no token
    provider, config = refresh_provider(current, provider_config(_Settings("mock")), broken)
    assert provider is current
    assert config == provider_config(broken)  # not retried (and logged) on every tick


def test_worker_rebuilds_its_provider_when_a_provider_setting_changes():
    from flighttracker.worker.__main__ import refresh_provider

    settings = _Settings("travelpayouts")
    settings.travelpayouts_token = SecretStr("old-token")
    current = get_provider(settings)
    config = provider_config(settings)
    assert refresh_provider(current, config, settings)[0] is current
    settings.travelpayouts_token = SecretStr("new-token")
    provider, _ = refresh_provider(current, config, settings)
    assert provider is not current
    assert provider.name == "travelpayouts"
