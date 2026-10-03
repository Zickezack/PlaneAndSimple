import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from flighttracker.i18n import Msg

_AIRPORT_CODE = re.compile(r"[A-Z]{3}")
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")
_SEPARATORS = re.compile(r"[\s,;]+")


class LocationKind(StrEnum):
    AIRPORT = "airport"
    COUNTRY = "country"


@dataclass(frozen=True, order=True)
class LocationRef:
    """An origin/destination entry: either an airport (IATA) or a whole country (ISO 3166-1)."""

    kind: LocationKind
    code: str

    @classmethod
    def airport(cls, code: str) -> "LocationRef":
        return cls(LocationKind.AIRPORT, code)

    @classmethod
    def country(cls, code: str) -> "LocationRef":
        return cls(LocationKind.COUNTRY, code)

    def __str__(self) -> str:
        return self.code


def parse_location_codes(raw: str) -> tuple[frozenset[LocationRef], list[Msg]]:
    """Parse user input like "ZRH, GVA ES": 3 letters = airport, 2 letters = country."""
    refs: set[LocationRef] = set()
    errors: list[Msg] = []
    for token in _SEPARATORS.split(raw.strip().upper()):
        if not token:
            continue
        if _AIRPORT_CODE.fullmatch(token):
            refs.add(LocationRef.airport(token))
        elif _COUNTRY_CODE.fullmatch(token):
            refs.add(LocationRef.country(token))
        else:
            errors.append(
                Msg('Invalid code "{code}" (airport: 3 letters, country: 2 letters).', code=token)
            )
    return frozenset(refs), errors


def format_location_codes(refs: Iterable[LocationRef]) -> str:
    """Inverse of `parse_location_codes`; countries first, then airports."""
    return ", ".join(ref.code for ref in sorted(refs, key=lambda r: (r.kind != "country", r.code)))


def country_codes(refs: Iterable[LocationRef]) -> set[str]:
    return {ref.code for ref in refs if ref.kind is LocationKind.COUNTRY}


def airport_codes(refs: Iterable[LocationRef]) -> set[str]:
    return {ref.code for ref in refs if ref.kind is LocationKind.AIRPORT}


def countries_of(refs: Iterable[LocationRef], airport_country: Mapping[str, str]) -> set[str]:
    """Countries touched by the given locations (airports resolved via `airport_country`)."""
    result: set[str] = set()
    for ref in refs:
        if ref.kind is LocationKind.COUNTRY:
            result.add(ref.code)
        elif ref.code in airport_country:
            result.add(airport_country[ref.code])
    return result


def describe_locations(
    refs: Iterable[LocationRef], airports_by_country: Mapping[str, Iterable[str]]
) -> str:
    """Like `format_location_codes`, but shows the included airports: "CH (GVA, ZRH), BCN"."""
    parts = []
    for ref in sorted(refs, key=lambda r: (r.kind != "country", r.code)):
        if ref.kind is LocationKind.COUNTRY:
            airports = ", ".join(sorted(airports_by_country.get(ref.code, ()))) or "–"
            parts.append(f"{ref.code} ({airports})")
        else:
            parts.append(ref.code)
    return ", ".join(parts)


def expand_to_airports(
    refs: Iterable[LocationRef], airports_by_country: Mapping[str, Iterable[str]]
) -> list[str]:
    """Resolve countries into their selected airports; returns sorted unique IATA codes."""
    result: set[str] = set()
    for ref in refs:
        if ref.kind is LocationKind.AIRPORT:
            result.add(ref.code)
        else:
            result.update(airports_by_country.get(ref.code, ()))
    return sorted(result)
