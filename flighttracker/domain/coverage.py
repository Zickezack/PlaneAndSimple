"""Detects whether a new Suchabo is already covered by, or can be merged into, an existing one."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from flighttracker.domain.filters import SearchFilters
from flighttracker.domain.locations import countries_of
from flighttracker.domain.spec import SearchSpec

MAX_DAYS_IN_MONTH = 31


class CoverageKind(StrEnum):
    COVERED = "covered"
    MERGEABLE = "mergeable"


@dataclass(frozen=True)
class CoverageMatch:
    search_id: int
    kind: CoverageKind


def is_compatible(new: SearchFilters, existing: SearchFilters) -> bool:
    """Filters that cannot be widened by merging must be identical."""
    return (
        new.trip_type == existing.trip_type
        and new.adults == existing.adults
        and new.children == existing.children
        and new.currency == existing.currency
    )


def _days_covered(new: int, existing: int) -> bool:
    """Day sampling spreads `days_per_month` evenly over the month, so more days are not a
    superset of fewer (4 days: 1, 8, 16, 24; 15 days: 1, 3, 5, …). Only the same count or every
    day of the month is certain to include the new search's departure days."""
    return new == existing or existing >= MAX_DAYS_IN_MONTH


def filters_covered(new: SearchFilters, existing: SearchFilters) -> bool:
    """True only if the existing Suchabo already fetches every price the new one would.

    Compares what is really queried and stored, not the ranges the user typed: the stay
    lengths actually requested (an empty stay means 7 days; long ranges are thinned out), and
    the same stop limit – only the cheapest itinerary within the limit is stored, so "any
    stops" says nothing about the cheapest direct flight.
    """
    return (
        is_compatible(new, existing)
        and new.cabin_classes <= existing.cabin_classes
        and new.max_stops == existing.max_stops
        and new.months_ahead <= existing.months_ahead
        and _days_covered(new.days_per_month, existing.days_per_month)
        and set(new.stay_lengths) <= set(existing.stay_lengths)
    )


def is_covered(new: SearchSpec, existing: SearchSpec) -> bool:
    """Countries count as their selected airports, so both specs must have complete selections."""
    return (
        set(new.origin_airports()) <= set(existing.origin_airports())
        and set(new.destination_airports()) <= set(existing.destination_airports())
        and filters_covered(new.filters, existing.filters)
    )


def classify(
    new: SearchSpec, existing: SearchSpec, airport_country: Mapping[str, str]
) -> CoverageKind | None:
    if is_covered(new, existing):
        return CoverageKind.COVERED
    same_origin_country = countries_of(new.origins, airport_country) & countries_of(
        existing.origins, airport_country
    )
    same_destination_country = countries_of(new.destinations, airport_country) & countries_of(
        existing.destinations, airport_country
    )
    if (
        same_origin_country
        and same_destination_country
        and is_compatible(new.filters, existing.filters)
    ):
        return CoverageKind.MERGEABLE
    return None


def find_overlaps(
    new: SearchSpec,
    existing: Mapping[int, SearchSpec],
    airport_country: Mapping[str, str],
) -> list[CoverageMatch]:
    """Matches sorted with COVERED first, then by search id."""
    matches = []
    for search_id, spec in existing.items():
        kind = classify(new, spec, airport_country)
        if kind is not None:
            matches.append(CoverageMatch(search_id=search_id, kind=kind))
    return sorted(matches, key=lambda m: (m.kind is not CoverageKind.COVERED, m.search_id))
