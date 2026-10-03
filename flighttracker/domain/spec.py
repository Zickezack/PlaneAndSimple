from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from flighttracker.domain.filters import SearchFilters
from flighttracker.domain.locations import LocationRef, country_codes, expand_to_airports


@dataclass(frozen=True)
class SearchSpec:
    """What a Suchabo tracks: where from, where to, and with which filters.

    `country_airports` holds the airports included for each country location. It is shared
    by origins and destinations and must contain every country of the spec before saving.
    """

    origins: frozenset[LocationRef]
    destinations: frozenset[LocationRef]
    filters: SearchFilters
    country_airports: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def country_codes(self) -> set[str]:
        return country_codes(self.origins) | country_codes(self.destinations)

    def countries_without_selection(self) -> set[str]:
        return {code for code in self.country_codes() if not self.country_airports.get(code)}

    def origin_airports(self) -> list[str]:
        return expand_to_airports(self.origins, self.country_airports)

    def destination_airports(self) -> list[str]:
        return expand_to_airports(self.destinations, self.country_airports)


def route_pairs(spec: SearchSpec) -> list[tuple[str, str]]:
    """All concrete airport-to-airport routes of a spec (countries expanded to their selection)."""
    destinations = spec.destination_airports()
    return [(o, d) for o in spec.origin_airports() for d in destinations if o != d]


def queries_per_poll(spec: SearchSpec) -> int:
    """Provider queries per poll: routes × cabin classes × months."""
    return len(route_pairs(spec)) * len(spec.filters.cabin_classes) * spec.filters.months_ahead


def requests_per_query(spec: SearchSpec) -> int:
    """Requests of a day-sampling provider per query: departure days × stay lengths."""
    return spec.filters.days_per_month * max(len(spec.filters.stay_lengths), 1)


def requests_per_poll(spec: SearchSpec, *, samples_days: bool) -> int:
    """Provider requests of one poll; providers that do not sample days need one per query."""
    queries = queries_per_poll(spec)
    return queries * requests_per_query(spec) if samples_days else queries


def trip_requests_per_poll(leg_specs: list[SearchSpec], window_days: int) -> int:
    """Upper bound for a Trip poll: every leg queried on every day of the window.

    Later legs only query dates that can connect, so real polls usually stay below it.
    """
    return sum(
        len(route_pairs(spec)) * len(spec.filters.cabin_classes) * window_days for spec in leg_specs
    )


def departure_months(months_ahead: int, today: date) -> list[date]:
    """First day of the current month and the following `months_ahead - 1` months."""
    months = []
    year, month = today.year, today.month
    for _ in range(months_ahead):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _min_or_none(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else min(a, b)


def _max_or_none(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else max(a, b)


def merge_filters(existing: SearchFilters, new: SearchFilters) -> SearchFilters:
    """Widest filters covering both. trip_type, adults and currency must already match."""
    return existing.model_copy(
        update={
            "cabin_classes": existing.cabin_classes | new.cabin_classes,
            "max_stops": _max_or_none(existing.max_stops, new.max_stops),
            "months_ahead": max(existing.months_ahead, new.months_ahead),
            "days_per_month": max(existing.days_per_month, new.days_per_month),
            "stay_days_min": _min_or_none(existing.stay_days_min, new.stay_days_min),
            "stay_days_max": _max_or_none(existing.stay_days_max, new.stay_days_max),
        }
    )


def merge_specs(existing: SearchSpec, new: SearchSpec) -> SearchSpec:
    countries = existing.country_airports.keys() | new.country_airports.keys()
    return SearchSpec(
        origins=existing.origins | new.origins,
        destinations=existing.destinations | new.destinations,
        filters=merge_filters(existing.filters, new.filters),
        country_airports={
            code: existing.country_airports.get(code, frozenset())
            | new.country_airports.get(code, frozenset())
            for code in countries
        },
    )
