from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from starlette.datastructures import FormData

from flighttracker.cli import render_dev_env
from flighttracker.domain.filters import CabinClass
from flighttracker.domain.locations import LocationRef
from flighttracker.services.airport_import import (
    parse_airports,
    parse_countries,
    parse_passengers,
    wikidata_user_agent,
)
from flighttracker.services.history import (
    MonthKey,
    MonthValue,
    PriceObservation,
    flight_key,
    flight_trends,
    pair_with_previous_year,
)
from flighttracker.web.forms import parse_search_form, values_from_spec


def form(**overrides) -> FormData:
    values = {
        "name": "Zürich → Spanien",
        "origins": "ZRH",
        "destinations": "ES",
        "trip_type": "round_trip",
        "max_stops": "any",
        "months_ahead": "6",
        "stay_days_min": "",
        "stay_days_max": "",
        "adults": "1",
        "currency": "chf",
        "poll_interval_hours": "6",
    } | overrides
    cabins = values.pop("cabin_classes", ["economy", "business"])
    return FormData([*values.items(), *(("cabin_classes", c) for c in cabins)])


class TestSearchForm:
    def test_valid_form(self):
        result = parse_search_form(form())
        assert result.errors == []
        data = result.data
        assert data.poll_interval_minutes == 360
        assert data.spec.origins == {LocationRef.airport("ZRH")}
        assert data.spec.destinations == {LocationRef.country("ES")}
        assert data.spec.filters.cabin_classes == {CabinClass.ECONOMY, CabinClass.BUSINESS}
        assert data.spec.filters.currency == "CHF"
        assert data.spec.filters.max_stops is None

    def test_platform_currency_and_interval_override_submitted_legacy_values(self):
        result = parse_search_form(
            form(currency="CHF", poll_interval_hours="1"),
            default_currency="EUR",
            poll_interval_minutes=1440,
        )
        assert result.data.spec.filters.currency == "EUR"
        assert result.data.poll_interval_minutes == 1440

    def test_roundtrip_through_values(self):
        data = parse_search_form(form(max_stops="0", stay_days_min="3")).data
        values = values_from_spec(data.name, data.spec, data.poll_interval_minutes)
        again = parse_search_form(
            FormData(
                [
                    *((k, v) for k, v in values.items() if k != "cabin_classes"),
                    *(("cabin_classes", c) for c in values["cabin_classes"]),
                ]
            )
        )
        assert again.data == data

    def test_collects_errors_in_both_languages(self):
        result = parse_search_form(form(name="", origins="ZURICH", cabin_classes=[]))
        assert result.data is None
        english = " ".join(e.render("en") for e in result.errors)
        assert "Please enter a name." in english
        assert 'Origin: Invalid code "ZURICH"' in english
        assert "cabin class" in english
        german = " ".join(e.render("de") for e in result.errors)
        assert "Bitte einen Namen angeben." in german
        assert "Abflug: Ungültiger Code „ZURICH“" in german
        assert "Kabinenklasse" in german

    def test_children_are_parsed(self):
        assert parse_search_form(form(children="2")).data.spec.filters.children == 2
        assert parse_search_form(form(children="")).data.spec.filters.children == 0
        result = parse_search_form(form(adults="8", children="2"))
        assert any("At most 9 passengers" in str(e) for e in result.errors)

    def test_countries_without_shown_checkboxes_are_unreviewed(self):
        result = parse_search_form(form(origins="CH, ZRH"))
        assert result.unreviewed_countries == {"CH", "ES"}
        assert result.data.spec.country_airports == {}

    def test_reads_airport_checkboxes(self):
        data = FormData(
            [
                *form(origins="CH").multi_items(),
                ("airport_countries", "CH,ES"),
                ("airports_CH", "zrh"),
                ("airports_CH", "GVA"),
                ("airports_ES", "BCN"),
            ]
        )
        result = parse_search_form(data)
        assert result.errors == []
        assert result.unreviewed_countries == set()
        assert result.data.spec.country_airports == {"CH": {"ZRH", "GVA"}, "ES": {"BCN"}}
        assert result.values["country_airports"] == {"CH": ["GVA", "ZRH"], "ES": ["BCN"]}

    def test_selection_of_removed_country_is_ignored(self):
        data = FormData(
            [
                *form(destinations="BCN").multi_items(),
                ("airport_countries", "ES"),
                ("airports_ES", "MAD"),
            ]
        )
        result = parse_search_form(data)
        assert result.data.spec.country_airports == {}

    def test_model_errors_are_reported(self):
        result = parse_search_form(form(trip_type="one_way", stay_days_min="3"))
        assert result.data is None
        assert any("only possible for round trips" in str(e) for e in result.errors)
        assert any("nur bei Hin- und Rückflug" in e.render("de") for e in result.errors)


class TestHistoryPairing:
    def test_pairs_with_previous_year_and_hides_past_months(self):
        observed = datetime(2026, 9, 1, tzinfo=UTC)

        def key(month):
            return MonthKey("ZRH", "BCN", "economy", "CHF", month)

        values = {
            key(date(2026, 11, 1)): MonthValue(Decimal("110"), observed),
            key(date(2025, 11, 1)): MonthValue(Decimal("100"), observed),
            key(date(2026, 12, 1)): MonthValue(Decimal("90"), observed),
            key(date(2026, 8, 1)): MonthValue(Decimal("80"), observed),
        }
        rows = pair_with_previous_year(values, today=date(2026, 9, 28))
        assert [r.key.month for r in rows] == [date(2026, 11, 1), date(2026, 12, 1)]
        assert rows[0].change_percent == Decimal("10.0")
        assert rows[1].previous_year is None
        assert rows[1].change_percent is None


COUNTRIES_CSV = """id,code,name,continent,wikipedia_link,keywords
1,CH,Switzerland,EU,,
2,ES,Spain,EU,,
"""
AIRPORTS_CSV = """id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,scheduled_service,gps_code,iata_code,local_code,home_link,wikipedia_link,keywords
1,LSZH,large_airport,Zurich Airport,0,0,0,EU,CH,CH-ZH,Zurich,yes,LSZH,ZRH,,,,
2,XXXX,closed,Old Zurich,0,0,0,EU,CH,CH-ZH,Zurich,no,,ZRH,,,,
3,LSZB,small_airport,Bern,0,0,0,EU,CH,CH-BE,Bern,no,LSZB,BRN,,,,
4,LSXX,heliport,No IATA,0,0,0,EU,CH,CH-BE,,no,,,,,,
5,KXXX,large_airport,Elsewhere,0,0,0,NA,US,US-NY,,yes,,JFK,,,,
"""


def test_parse_ourairports():
    countries = parse_countries(COUNTRIES_CSV)
    assert {c["code"] for c in countries} == {"CH", "ES"}
    airports = {a["iata_code"]: a for a in parse_airports(AIRPORTS_CSV, {"CH", "ES"})}
    assert set(airports) == {"ZRH", "BRN"}
    assert airports["ZRH"]["name"] == "Zurich Airport"
    assert airports["ZRH"]["has_scheduled_service"] is True
    assert airports["BRN"]["city"] == "Bern"


def test_parse_wikidata_passengers():
    payload = {
        "results": {
            "bindings": [
                {"iata": {"value": "ZRH"}, "passengers": {"value": "32593966"}},
                {"iata": {"value": "gva"}, "passengers": {"value": "1.79E7"}},
                {"iata": {"value": "TOOLONG"}, "passengers": {"value": "5"}},
                {"iata": {"value": "BRN"}, "passengers": {"value": "n/a"}},
                {"iata": {"value": "LUG"}},
            ]
        }
    }
    assert parse_passengers(payload) == {"ZRH": 32593966, "GVA": 17900000}


def test_wikidata_user_agent_contains_contact():
    assert "ops@example.org" in wikidata_user_agent("ops@example.org")


def test_render_dev_env_sets_fresh_secrets():
    example = (
        "SECRET_KEY=change-me\nADMIN_PASSWORD_HASH=\nSESSION_COOKIE_SECURE=true\nWEB_PORT=8000\n"
    )
    first = render_dev_env(example, "pw-one-123456")
    second = render_dev_env(example, "pw-two-123456")
    assert "change-me" not in first
    assert "SESSION_COOKIE_SECURE=false" in first
    assert "ADMIN_PASSWORD_HASH=scrypt:" in first
    assert "WEB_PORT=8000" in first
    assert first.split("SECRET_KEY=")[1][:64] != second.split("SECRET_KEY=")[1][:64]


def test_days_per_month_is_parsed():
    assert parse_search_form(form(days_per_month="15")).data.spec.filters.days_per_month == 15
    assert parse_search_form(form(days_per_month="")).data.spec.filters.days_per_month == 4


def test_flight_trend_has_explicit_daily_weekly_monthly_changes():
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    point = SimpleNamespace(
        origin="ZRH",
        destination="BCN",
        cabin_class="economy",
        currency="CHF",
        departure_date=date(2026, 11, 20),
        return_date=None,
        observed_at=now,
        price=Decimal("150.00"),
    )
    observations = [
        PriceObservation(Decimal("200.00"), datetime(2026, 9, 1, 12, tzinfo=UTC)),
        PriceObservation(Decimal("180.00"), datetime(2026, 9, 25, 12, tzinfo=UTC)),
        PriceObservation(Decimal("170.00"), datetime(2026, 10, 1, 12, tzinfo=UTC)),
        PriceObservation(Decimal("150.00"), now),
    ]

    trend = flight_trends([point], {flight_key(point): observations})[flight_key(point)]

    assert [(change.period, change.amount) for change in trend.changes] == [
        ("1d", Decimal("-20.00")),
        ("1w", Decimal("-30.00")),
        ("1m", Decimal("-50.00")),
    ]
