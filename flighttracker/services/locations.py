"""Airport and country lookups: selectable airports per country, suggestions, names."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from flighttracker.domain.locations import LocationKind
from flighttracker.domain.spec import SearchSpec
from flighttracker.i18n import DEFAULT_LOCALE, country_catalog, country_name
from flighttracker.models import (
    Airport,
    Country,
)

# Airports that can be selected for a country: scheduled service and one of these OurAirports types.
SELECTABLE_AIRPORT_TYPES = ("large_airport", "medium_airport")


@dataclass(frozen=True)
class AirportOption:
    iata_code: str
    name: str
    city: str | None
    passengers: int | None


def airport_options(session: Session, countries: set[str]) -> dict[str, list[AirportOption]]:
    """Selectable airports per country, largest first (passengers, then OurAirports type)."""
    if not countries:
        return {}
    type_rank = case({"large_airport": 0}, value=Airport.airport_type, else_=1)
    rows = session.execute(
        select(Airport)
        .where(
            Airport.country_code.in_(countries),
            Airport.has_scheduled_service.is_(True),
            Airport.airport_type.in_(SELECTABLE_AIRPORT_TYPES),
        )
        .order_by(Airport.passengers.desc().nulls_last(), type_rank, Airport.iata_code)
    ).scalars()
    result: dict[str, list[AirportOption]] = defaultdict(list)
    for airport in rows:
        result[airport.country_code].append(
            AirportOption(airport.iata_code, airport.name, airport.city, airport.passengers)
        )
    return dict(result)


def with_default_airports(
    spec: SearchSpec, options: Mapping[str, list[AirportOption]], count: int
) -> SearchSpec:
    """Preselect the `count` largest airports for every country that has no selection yet."""
    selection = {
        code: airports
        for code, airports in spec.country_airports.items()
        if code in spec.country_codes()
    }
    for code in spec.countries_without_selection():
        selection[code] = frozenset(o.iata_code for o in options.get(code, [])[:count])
    return replace(spec, country_airports=selection)


@dataclass(frozen=True)
class LocationSuggestion:
    code: str
    kind: LocationKind
    label: str


def suggest_locations(
    session: Session, query: str, limit: int = 10, locale: str = DEFAULT_LOCALE
) -> list[LocationSuggestion]:
    """Countries and airports (scheduled service) matching a code, name or city.

    Countries match their English name and their name in `locale` and are labelled in `locale`.
    Order: exact code, then matches at the start of code/name/city, then larger airports first.
    """
    term = query.strip()
    if not term:
        return []
    upper, pattern, folded = term.upper(), f"%{term}%", term.casefold()
    localized_matches = [
        code for code, name in country_catalog(locale).items() if folded in name.casefold()
    ]
    matching = session.scalars(
        select(Country).where(
            or_(
                Country.code == upper,
                Country.name.ilike(pattern),
                Country.code.in_(localized_matches),
            )
        )
    ).all()

    def label(country: Country) -> str:
        return country_name(country.code, country.name, locale)

    def starts_with_term(country: Country) -> bool:
        names = (label(country), country.name)
        return any(name.casefold().startswith(folded) for name in names)

    countries = sorted(
        matching, key=lambda c: (c.code != upper, not starts_with_term(c), label(c))
    )[:limit]
    airports = session.scalars(
        select(Airport)
        .where(
            Airport.has_scheduled_service.is_(True),
            or_(
                Airport.iata_code.startswith(upper),
                Airport.name.ilike(pattern),
                Airport.city.ilike(pattern),
            ),
        )
        .order_by(
            case(
                (Airport.iata_code == upper, 0),
                (
                    or_(
                        Airport.iata_code.startswith(upper),
                        Airport.city.ilike(f"{term}%"),
                        Airport.name.ilike(f"{term}%"),
                    ),
                    1,
                ),
                else_=2,
            ),
            Airport.passengers.desc().nulls_last(),
            Airport.name,
        )
        .limit(limit)
    ).all()
    suggestions = [
        LocationSuggestion(c.code, LocationKind.COUNTRY, label(c)) for c in countries
    ] + [
        LocationSuggestion(
            a.iata_code, LocationKind.AIRPORT, f"{a.name}{f', {a.city}' if a.city else ''}"
        )
        for a in airports
    ]
    # Exact code matches first (a country "ES" or an airport "ZRH"), then countries, then airports.
    suggestions.sort(key=lambda s: s.code != upper)
    return suggestions[:limit]


def country_names(
    session: Session, codes: set[str], locale: str = DEFAULT_LOCALE
) -> dict[str, str]:
    if not codes:
        return {}
    rows = session.execute(select(Country.code, Country.name).where(Country.code.in_(codes)))
    return {code: country_name(code, name, locale) for code, name in rows}


def load_airport_countries(session: Session, codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    rows = session.execute(
        select(Airport.iata_code, Airport.country_code).where(Airport.iata_code.in_(codes))
    )
    return {key: value for key, value in rows}
