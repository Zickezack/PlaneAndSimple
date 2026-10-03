from datetime import date

import pytest
from pydantic import ValidationError

from flighttracker.domain.coverage import CoverageKind, classify, filters_covered, find_overlaps
from flighttracker.domain.filters import CabinClass, SearchFilters, TripType, stay_lengths
from flighttracker.domain.locations import (
    LocationRef,
    describe_locations,
    expand_to_airports,
    format_location_codes,
    parse_location_codes,
)
from flighttracker.domain.spec import (
    SearchSpec,
    departure_months,
    merge_specs,
    queries_per_poll,
    requests_per_query,
    route_pairs,
)

A = LocationRef.airport
C = LocationRef.country
AIRPORT_COUNTRY = {"ZRH": "CH", "GVA": "CH", "BSL": "CH", "BCN": "ES", "MAD": "ES", "PMI": "ES"}
AIRPORTS_BY_COUNTRY = {"CH": ["ZRH", "GVA", "BSL"], "ES": ["BCN", "MAD", "PMI"]}


def spec(origins, destinations, airports=None, **filters) -> SearchSpec:
    """Countries include all their test airports unless `airports` narrows the selection."""
    refs = set(origins) | set(destinations)
    selection = {
        ref.code: frozenset(AIRPORTS_BY_COUNTRY.get(ref.code, ()))
        for ref in refs
        if ref.kind == "country"
    } | {code: frozenset(codes) for code, codes in (airports or {}).items()}
    return SearchSpec(
        frozenset(origins), frozenset(destinations), SearchFilters(**filters), selection
    )


class TestStayLengths:
    @pytest.mark.parametrize(
        ("minimum", "maximum", "expected"),
        [
            (None, None, [7]),
            (10, None, [10]),
            (None, 5, [5]),
            (7, 10, [7, 8, 9, 10]),
            (7, 30, [7, 10, 14, 17, 20, 23, 27, 30]),
        ],
    )
    def test_range_thinned_to_eight(self, minimum, maximum, expected):
        assert stay_lengths(TripType.ROUND_TRIP, minimum, maximum) == expected

    def test_one_way_has_none(self):
        assert stay_lengths(TripType.ONE_WAY, None, None) == []

    def test_requests_per_query(self):
        s = spec([A("ZRH")], [A("BCN")], stay_days_min=7, stay_days_max=9, days_per_month=8)
        assert requests_per_query(s) == 8 * 3


class TestFilters:
    def test_defaults(self):
        f = SearchFilters()
        assert f.trip_type is TripType.ROUND_TRIP
        assert f.cabin_classes == {CabinClass.ECONOMY}
        assert f.max_stops is None

    def test_json_is_sorted_and_roundtrips(self):
        f = SearchFilters(cabin_classes={CabinClass.FIRST, CabinClass.BUSINESS})
        data = f.to_json()
        assert data["cabin_classes"] == ["business", "first"]
        assert SearchFilters.model_validate(data) == f

    def test_stay_not_allowed_for_one_way(self):
        with pytest.raises(ValidationError, match="only possible for round trips"):
            SearchFilters(trip_type=TripType.ONE_WAY, stay_days_min=3)

    def test_stay_min_greater_than_max(self):
        with pytest.raises(ValidationError, match="minimum stay is longer"):
            SearchFilters(stay_days_min=10, stay_days_max=5)

    def test_needs_a_cabin(self):
        with pytest.raises(ValidationError):
            SearchFilters(cabin_classes=set())

    def test_unknown_keys_rejected(self):
        with pytest.raises(ValidationError):
            SearchFilters.model_validate({"foo": 1})


class TestLocations:
    def test_parse_mixed_input(self):
        refs, errors = parse_location_codes(" zrh, GVA;es  ")
        assert refs == {A("ZRH"), A("GVA"), C("ES")}
        assert errors == []

    def test_parse_reports_invalid_tokens(self):
        refs, errors = parse_location_codes("ZRH, ZURICH, 1A")
        assert refs == {A("ZRH")}
        assert len(errors) == 2

    def test_parse_empty(self):
        assert parse_location_codes("   ") == (frozenset(), [])

    def test_format_countries_first(self):
        assert format_location_codes({A("ZRH"), C("ES"), A("BCN")}) == "ES, BCN, ZRH"

    def test_expand_countries(self):
        assert expand_to_airports({C("CH"), A("BCN")}, AIRPORTS_BY_COUNTRY) == [
            "BCN",
            "BSL",
            "GVA",
            "ZRH",
        ]

    def test_expand_unknown_country_yields_nothing(self):
        assert expand_to_airports({C("XX")}, AIRPORTS_BY_COUNTRY) == []

    def test_describe_shows_selected_airports(self):
        text = describe_locations({C("CH"), A("BCN")}, {"CH": frozenset({"ZRH", "GVA"})})
        assert text == "CH (GVA, ZRH), BCN"


class TestSpec:
    def test_route_pairs_excludes_same_airport(self):
        s = spec([A("ZRH"), A("BCN")], [C("ES")])
        pairs = route_pairs(s)
        assert ("BCN", "BCN") not in pairs
        assert len(pairs) == 5

    def test_route_pairs_use_only_selected_airports(self):
        s = spec([C("CH")], [A("BCN")], airports={"CH": {"ZRH"}})
        assert route_pairs(s) == [("ZRH", "BCN")]

    def test_countries_without_selection(self):
        s = SearchSpec(frozenset({C("CH")}), frozenset({A("BCN")}), SearchFilters(), {})
        assert s.countries_without_selection() == {"CH"}
        assert route_pairs(s) == []

    def test_queries_per_poll(self):
        s = spec(
            [C("CH")],
            [A("BCN")],
            airports={"CH": {"ZRH", "GVA"}},
            cabin_classes={CabinClass.ECONOMY, CabinClass.BUSINESS},
            months_ahead=3,
        )
        assert queries_per_poll(s) == 2 * 2 * 3

    def test_departure_months_wrap_year(self):
        assert departure_months(3, date(2026, 11, 17)) == [
            date(2026, 11, 1),
            date(2026, 12, 1),
            date(2027, 1, 1),
        ]

    def test_merge_widens_filters(self):
        existing = spec(
            [A("ZRH")], [A("BCN")], max_stops=0, months_ahead=3, stay_days_min=5, stay_days_max=7
        )
        new = spec(
            [A("GVA")],
            [A("MAD")],
            cabin_classes={CabinClass.BUSINESS},
            max_stops=1,
            months_ahead=6,
            stay_days_min=3,
            stay_days_max=14,
        )
        merged = merge_specs(existing, new)
        assert merged.origins == {A("ZRH"), A("GVA")}
        assert merged.destinations == {A("BCN"), A("MAD")}
        f = merged.filters
        assert f.cabin_classes == {CabinClass.ECONOMY, CabinClass.BUSINESS}
        assert (f.max_stops, f.months_ahead, f.stay_days_min, f.stay_days_max) == (1, 6, 3, 14)

    def test_merge_unions_airport_selections(self):
        merged = merge_specs(
            spec([C("CH")], [A("BCN")], airports={"CH": {"ZRH"}}),
            spec([C("CH")], [A("MAD")], airports={"CH": {"GVA"}}),
        )
        assert merged.country_airports == {"CH": {"ZRH", "GVA"}}

    def test_merge_unlimited_wins(self):
        merged = merge_specs(
            spec([A("ZRH")], [A("BCN")], max_stops=0), spec([A("ZRH")], [A("BCN")])
        )
        assert merged.filters.max_stops is None


class TestCoverage:
    def test_airport_covered_by_country_search(self):
        existing = spec([C("CH")], [C("ES")])
        new = spec([A("ZRH")], [A("BCN")])
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.COVERED

    def test_country_not_covered_by_airports(self):
        existing = spec([A("ZRH")], [A("BCN")])
        new = spec([C("CH")], [A("BCN")])
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.MERGEABLE

    def test_country_covered_by_its_selected_airports(self):
        existing = spec([A("ZRH"), A("GVA")], [A("BCN")])
        new = spec([C("CH")], [A("BCN")], airports={"CH": {"ZRH", "GVA"}})
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.COVERED

    def test_airport_outside_selection_is_not_covered(self):
        existing = spec([C("CH")], [C("ES")], airports={"CH": {"ZRH"}})
        new = spec([A("GVA")], [A("BCN")])
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.MERGEABLE

    def test_same_countries_different_airports_is_mergeable(self):
        existing = spec([A("ZRH")], [A("BCN")])
        new = spec([A("GVA")], [A("PMI")])
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.MERGEABLE

    def test_broader_filters_are_not_covered_but_mergeable(self):
        existing = spec([C("CH")], [C("ES")], max_stops=0)
        new = spec([A("ZRH")], [A("BCN")], max_stops=1)
        assert classify(new, existing, AIRPORT_COUNTRY) is CoverageKind.MERGEABLE

    def test_different_children_are_not_mergeable(self):
        existing = spec([A("ZRH")], [A("BCN")])
        new = spec([A("GVA")], [A("MAD")], children=1)
        assert classify(new, existing, AIRPORT_COUNTRY) is None

    def test_different_trip_type_is_not_mergeable(self):
        existing = spec([A("ZRH")], [A("BCN")])
        new = spec([A("GVA")], [A("MAD")], trip_type=TripType.ONE_WAY)
        assert classify(new, existing, AIRPORT_COUNTRY) is None

    def test_other_countries_do_not_match(self):
        existing = spec([A("ZRH")], [A("BCN")])
        new = spec([A("BCN")], [A("ZRH")])
        assert classify(new, existing, AIRPORT_COUNTRY) is None

    @pytest.mark.parametrize(
        ("new", "existing", "covered"),
        [
            ({"max_stops": 0}, {"max_stops": 1}, True),
            ({"max_stops": None}, {"max_stops": 1}, False),
            ({"max_stops": 2}, {"max_stops": None}, True),
            ({"stay_days_min": 5, "stay_days_max": 7}, {}, True),
            ({}, {"stay_days_min": 5}, False),
            ({"stay_days_min": 3}, {"stay_days_min": 5}, False),
            ({"months_ahead": 12}, {"months_ahead": 6}, False),
            ({"currency": "EUR"}, {}, False),
        ],
    )
    def test_filters_covered(self, new, existing, covered):
        assert filters_covered(SearchFilters(**new), SearchFilters(**existing)) is covered

    def test_find_overlaps_orders_covered_first(self):
        new = spec([A("ZRH")], [A("BCN")])
        existing = {
            1: spec([A("GVA")], [A("MAD")]),
            2: spec([C("CH")], [C("ES")]),
            3: spec([A("BCN")], [A("ZRH")]),
        }
        matches = find_overlaps(new, existing, AIRPORT_COUNTRY)
        assert [(m.search_id, m.kind) for m in matches] == [
            (2, CoverageKind.COVERED),
            (1, CoverageKind.MERGEABLE),
        ]
