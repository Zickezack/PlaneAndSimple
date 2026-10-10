from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, case, delete, or_, select
from sqlalchemy.orm import Session, object_session, selectinload

from flighttracker.domain.coverage import CoverageKind, find_overlaps, is_covered
from flighttracker.domain.filters import SearchFilters
from flighttracker.domain.locations import LocationKind, LocationRef, airport_codes, country_codes
from flighttracker.domain.spec import SearchSpec, merge_specs, route_pairs
from flighttracker.i18n import DEFAULT_LOCALE, Msg, country_catalog, country_name
from flighttracker.models import (
    Airport,
    Country,
    LocationRole,
    Search,
    SearchLocation,
    SearchRevision,
    SearchStatus,
    TripLeg,
)
from flighttracker.services.jobs import wake_worker

# Airports that can be selected for a country: scheduled service and one of these OurAirports types.
SELECTABLE_AIRPORT_TYPES = ("large_airport", "medium_airport")


class SearchValidationError(ValueError):
    def __init__(self, errors: list[Msg]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


@dataclass(frozen=True)
class SearchInput:
    name: str
    spec: SearchSpec
    poll_interval_minutes: int


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AirportOption:
    iata_code: str
    name: str
    city: str | None
    passengers: int | None


def spec_of(search: Search) -> SearchSpec:
    origins = frozenset(loc.ref for loc in search.locations if loc.role is LocationRole.ORIGIN)
    destinations = frozenset(
        loc.ref for loc in search.locations if loc.role is LocationRole.DESTINATION
    )
    country_airports: dict[str, frozenset[str]] = {}
    for loc in search.locations:
        if loc.country_code is not None:
            selected = frozenset(loc.airport_codes or ())
            country_airports[loc.country_code] = (
                country_airports.get(loc.country_code, frozenset()) | selected
            )
    return SearchSpec(
        origins, destinations, SearchFilters.model_validate(search.filters), country_airports
    )


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


def _selection_errors(session: Session, spec: SearchSpec) -> list[Msg]:
    errors = [
        Msg('Please select at least one airport for "{code}".', code=code)
        for code in sorted(spec.countries_without_selection())
    ]
    options = airport_options(session, spec.country_codes())
    for code in sorted(spec.country_codes()):
        allowed = {o.iata_code for o in options.get(code, [])}
        for iata in sorted(spec.country_airports.get(code, frozenset()) - allowed):
            errors.append(
                Msg('"{iata}" is not a selectable airport in "{code}".', iata=iata, code=code)
            )
    return errors


def validate_input(session: Session, data: SearchInput, *, max_route_pairs: int) -> None:
    spec = data.spec
    errors: list[Msg] = []
    if not spec.origins:
        errors.append(Msg("At least one origin is required."))
    if not spec.destinations:
        errors.append(Msg("At least one destination is required."))

    all_locations = spec.origins | spec.destinations
    wanted_airports = airport_codes(all_locations)
    wanted_countries = country_codes(all_locations)
    known_airports = set(
        session.scalars(select(Airport.iata_code).where(Airport.iata_code.in_(wanted_airports)))
    )
    known_countries = set(
        session.scalars(select(Country.code).where(Country.code.in_(wanted_countries)))
    )
    for code in sorted(wanted_airports - known_airports):
        errors.append(Msg('Unknown airport "{code}".', code=code))
    for code in sorted(wanted_countries - known_countries):
        errors.append(Msg('Unknown country "{code}".', code=code))

    if not errors:
        errors.extend(_selection_errors(session, spec))
    if not errors:
        pairs = len(route_pairs(spec))
        if pairs == 0:
            errors.append(Msg("No route found – there are no airports for these countries."))
        elif pairs > max_route_pairs:
            errors.append(
                Msg(
                    "Too many routes ({pairs}, at most {max}). Please select fewer airports.",
                    pairs=pairs,
                    max=max_route_pairs,
                )
            )
    if errors:
        raise SearchValidationError(errors)


def _sync_locations(search: Search, spec: SearchSpec) -> None:
    """Diff instead of replace, so unchanged rows are kept (avoids unique-constraint churn)."""
    desired = {(LocationRole.ORIGIN, ref) for ref in spec.origins} | {
        (LocationRole.DESTINATION, ref) for ref in spec.destinations
    }
    current = {(loc.role, loc.ref): loc for loc in search.locations}
    for key, location in current.items():
        if key not in desired:
            search.locations.remove(location)
        elif location.country_code is not None:
            selected = _selected_airports(spec, location.country_code)
            if location.airport_codes != selected:
                location.airport_codes = selected
    for role, ref in sorted(desired - current.keys()):
        is_country = ref.kind is LocationKind.COUNTRY
        search.locations.append(
            SearchLocation(
                role=role,
                airport_code=None if is_country else ref.code,
                country_code=ref.code if is_country else None,
                airport_codes=_selected_airports(spec, ref.code) if is_country else None,
            )
        )


def _selected_airports(spec: SearchSpec, country: str) -> list[str]:
    return sorted(spec.country_airports.get(country, frozenset()))


def _snapshot(search: Search, spec: SearchSpec) -> dict:
    def refs(values: frozenset[LocationRef]) -> list[dict]:
        return [{"kind": ref.kind.value, "code": ref.code} for ref in sorted(values)]

    return {
        "name": search.name,
        "poll_interval_minutes": search.poll_interval_minutes,
        "origins": refs(spec.origins),
        "destinations": refs(spec.destinations),
        "country_airports": {
            code: _selected_airports(spec, code) for code in sorted(spec.country_codes())
        },
        "filters": spec.filters.to_json(),
    }


def _record_revision(session: Session, search: Search, spec: SearchSpec) -> SearchRevision:
    revision = SearchRevision(
        search_id=search.id, revision_no=search.revision_no, snapshot=_snapshot(search, spec)
    )
    session.add(revision)
    session.flush()
    return revision


def current_revision_id(session: Session, search: Search) -> int:
    return session.scalars(
        select(SearchRevision.id).where(
            SearchRevision.search_id == search.id,
            SearchRevision.revision_no == search.revision_no,
        )
    ).one()


def create_search(
    session: Session,
    data: SearchInput,
    *,
    max_route_pairs: int,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> Search:
    validate_input(session, data, max_route_pairs=max_route_pairs)
    now = now or _utcnow()
    search = Search(
        name=data.name,
        owner_id=owner_id,
        status=SearchStatus.ACTIVE,
        filters=data.spec.filters.to_json(),
        poll_interval_minutes=data.poll_interval_minutes,
        revision_no=1,
        # The worker polls right away, so first prices arrive within minutes.
        next_poll_at=now,
    )
    _sync_locations(search, data.spec)
    session.add(search)
    session.flush()
    _record_revision(session, search, data.spec)
    wake_worker(session)
    return search


def update_search(
    session: Session,
    search: Search,
    data: SearchInput,
    *,
    max_route_pairs: int,
    now: datetime | None = None,
) -> bool:
    """Apply changes as a new revision. Existing price history is never touched.

    Returns False if nothing changed (no new revision).
    """
    validate_input(session, data, max_route_pairs=max_route_pairs)
    old_spec = spec_of(search)
    before = _snapshot(search, old_spec)

    search.name = data.name
    search.poll_interval_minutes = data.poll_interval_minutes
    search.filters = data.spec.filters.to_json()
    _sync_locations(search, data.spec)
    if _snapshot(search, data.spec) == before:
        return False

    search.revision_no += 1
    session.flush()
    _record_revision(session, search, data.spec)

    # Broadened: poll right away instead of waiting for the next interval.
    if not is_covered(data.spec, old_spec) and search.status is SearchStatus.ACTIVE:
        search.next_poll_at = now or _utcnow()
        wake_worker(session)
    return True


def merge_into(
    session: Session,
    target: Search,
    new_spec: SearchSpec,
    *,
    max_route_pairs: int,
    now: datetime | None = None,
) -> bool:
    """Add the new locations to `target` and widen its filters to cover both."""
    merged = merge_specs(spec_of(target), new_spec)
    data = SearchInput(target.name, merged, target.poll_interval_minutes)
    return update_search(session, target, data, max_route_pairs=max_route_pairs, now=now)


def pause_search(search: Search) -> None:
    search.status = SearchStatus.PAUSED


def resume_search(search: Search, now: datetime | None = None) -> None:
    search.status = SearchStatus.ACTIVE
    search.next_poll_at = now or _utcnow()
    session = object_session(search)
    if session is not None:
        wake_worker(session)


def archive_search(search: Search, now: datetime | None = None) -> None:
    """Soft delete: the Suchabo disappears from the list, all data is kept."""
    search.archived_at = now or _utcnow()


def restore_search(search: Search) -> None:
    search.archived_at = None


def delete_search_permanently(session: Session, search: Search) -> None:
    """Hard delete – the database cascades to locations, revisions, price history and jobs."""
    session.execute(delete(Search).where(Search.id == search.id))


def get_search(session: Session, search_id: int) -> Search | None:
    return session.scalars(
        select(Search).where(Search.id == search_id).options(selectinload(Search.locations))
    ).first()


def list_searches(
    session: Session, *, archived: bool = False, visible: ColumnElement[bool] | None = None
) -> list[Search]:
    """`visible`: condition from `services.access.visible_searches` (None = all)."""
    condition = Search.archived_at.is_not(None) if archived else Search.archived_at.is_(None)
    statement = select(Search).where(condition)
    if visible is not None:
        statement = statement.where(visible)
    return list(
        session.scalars(
            statement.options(selectinload(Search.locations)).order_by(Search.name, Search.id)
        )
    )


def _airport_countries_for(session: Session, *specs: SearchSpec) -> dict[str, str]:
    codes: set[str] = set()
    for spec in specs:
        codes |= airport_codes(spec.origins | spec.destinations)
    return load_airport_countries(session, codes)


def find_overlapping_searches(
    session: Session,
    spec: SearchSpec,
    *,
    exclude_id: int | None = None,
    visible: ColumnElement[bool] | None = None,
) -> list[tuple[Search, CoverageKind]]:
    """Only among the Suchabos the user may see (`visible`), so no other user's is revealed."""
    trip_search_ids = set(session.scalars(select(TripLeg.search_id)))
    candidates = {
        s.id: s
        for s in list_searches(session, visible=visible)
        if s.id != exclude_id and s.id not in trip_search_ids
    }
    specs = {search_id: spec_of(search) for search_id, search in candidates.items()}
    airport_country = _airport_countries_for(session, spec, *specs.values())
    return [
        (candidates[match.search_id], match.kind)
        for match in find_overlaps(spec, specs, airport_country)
    ]
