"""Export/import of Suchabos and Trips as JSON (global export/import plus per-search export).

Import only ever **adds** new records – matched by name + creation time for the Suchabo
itself, and by their natural identity for price history / query log rows – so running it
twice, or importing overlapping exports from several dev servers, never overwrites or
duplicates anything. Revision history is not preserved on import: an imported Suchabo that
did not already exist gets one fresh revision holding its current filters, and every
imported price observation is attached to that revision.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from flighttracker.domain.filters import CabinClass, SearchFilters
from flighttracker.models import (
    LocationRole,
    PriceHistory,
    PriceSource,
    QueryLog,
    QueryOutcome,
    Search,
    SearchLocation,
    SearchRevision,
    SearchStatus,
    Trip,
    TripLeg,
)

FORMAT_VERSION = 2
SUPPORTED_FORMATS = {1, 2}


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _price_to_dict(price: PriceHistory) -> dict:
    return {
        "source": price.source.value,
        "provider": price.provider,
        "origin_iata": price.origin_iata,
        "destination_iata": price.destination_iata,
        "departure_date": _iso(price.departure_date),
        "return_date": _iso(price.return_date),
        "cabin_class": price.cabin_class.value,
        "stops": price.stops,
        "price": str(price.price),
        "currency": price.currency,
        "airline": price.airline,
        "details": price.details,
        "observed_at": _iso(price.observed_at),
    }


def _log_to_dict(entry: QueryLog) -> dict:
    return {
        "provider": entry.provider,
        "origin_iata": entry.origin_iata,
        "destination_iata": entry.destination_iata,
        "departure_month": _iso(entry.departure_month),
        "cabin_class": entry.cabin_class.value,
        "outcome": entry.outcome.value,
        "quotes_found": entry.quotes_found,
        "quotes_stored": entry.quotes_stored,
        "error": entry.error,
        "results": entry.results,
        "started_at": _iso(entry.started_at),
        "duration_ms": entry.duration_ms,
    }


def _export_one(session: Session, search: Search) -> dict:
    prices = session.scalars(
        select(PriceHistory).where(PriceHistory.search_id == search.id).order_by(PriceHistory.id)
    ).all()
    logs = session.scalars(
        select(QueryLog).where(QueryLog.search_id == search.id).order_by(QueryLog.id)
    ).all()
    return {
        "name": search.name,
        "status": search.status.value,
        "filters": search.filters,
        "poll_interval_minutes": search.poll_interval_minutes,
        "created_at": _iso(search.created_at),
        "archived_at": _iso(search.archived_at),
        "locations": [
            {
                "role": location.role.value,
                "airport_code": location.airport_code,
                "country_code": location.country_code,
                "airport_codes": location.airport_codes,
            }
            for location in search.locations
        ],
        "price_history": [_price_to_dict(price) for price in prices],
        "query_log": [_log_to_dict(entry) for entry in logs],
    }


def _search_reference(search: Search) -> dict:
    return {"name": search.name, "created_at": _iso(search.created_at)}


def _export_trip(trip: Trip) -> dict:
    return {
        "name": trip.name,
        "starts_on": _iso(trip.starts_on),
        "ends_on": _iso(trip.ends_on),
        "status": trip.status.value,
        "poll_interval_minutes": trip.poll_interval_minutes,
        "next_poll_at": _iso(trip.next_poll_at),
        "last_polled_at": _iso(trip.last_polled_at),
        "created_at": _iso(trip.created_at),
        "updated_at": _iso(trip.updated_at),
        "archived_at": _iso(trip.archived_at),
        "legs": [
            {
                "position": leg.position,
                "search": _search_reference(leg.search),
                "min_layover_days": leg.min_layover_days,
                "max_layover_days": leg.max_layover_days,
            }
            for leg in trip.legs
        ],
    }


def export_search(session: Session, search_id: int) -> dict:
    """JSON-ready export of a single Suchabo."""
    search = session.get(Search, search_id)
    if search is None:
        raise ValueError(f"Tracked search {search_id} not found.")
    return {
        "format": FORMAT_VERSION,
        "exported_at": _iso(datetime.now(UTC)),
        "searches": [_export_one(session, search)],
    }


def export_all(session: Session) -> dict:
    """JSON-ready export of all Suchabos and Trips, including archived records and history."""
    searches = session.scalars(select(Search).order_by(Search.id)).all()
    trips = session.scalars(
        select(Trip).options(selectinload(Trip.legs).selectinload(TripLeg.search)).order_by(Trip.id)
    ).all()
    return {
        "format": FORMAT_VERSION,
        "exported_at": _iso(datetime.now(UTC)),
        "searches": [_export_one(session, search) for search in searches],
        "trips": [_export_trip(trip) for trip in trips],
    }


@dataclass
class ImportResult:
    searches_created: int = 0
    searches_matched: int = 0
    prices_added: int = 0
    logs_added: int = 0
    trips_created: int = 0
    trips_matched: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.searches_created or self.prices_added or self.logs_added or self.trips_created
        )


def _find_existing_search(session: Session, entry: dict) -> Search | None:
    created_at = entry.get("created_at")
    if not created_at:
        return None
    return session.scalar(
        select(Search).where(
            Search.name == entry["name"], Search.created_at == datetime.fromisoformat(created_at)
        )
    )


def _get_or_create_search(
    session: Session, entry: dict, result: ImportResult
) -> tuple[Search, int]:
    """Returns the (matched or newly created) Suchabo and the revision id new prices attach to."""
    existing = _find_existing_search(session, entry)
    if existing is not None:
        result.searches_matched += 1
        revision = session.scalars(
            select(SearchRevision)
            .where(SearchRevision.search_id == existing.id)
            .order_by(SearchRevision.revision_no.desc())
            .limit(1)
        ).first()
        return existing, revision.id

    SearchFilters.model_validate(entry["filters"])  # pages could not render invalid filters
    search = Search(
        name=entry["name"],
        status=SearchStatus(entry.get("status", SearchStatus.ACTIVE.value)),
        filters=entry["filters"],
        poll_interval_minutes=entry["poll_interval_minutes"],
        revision_no=1,
        # Due right away, like a newly created Suchabo; the scheduler skips inactive ones.
        next_poll_at=datetime.now(UTC),
    )
    if entry.get("created_at"):
        search.created_at = datetime.fromisoformat(entry["created_at"])
    if entry.get("archived_at"):
        search.archived_at = datetime.fromisoformat(entry["archived_at"])
    session.add(search)
    session.flush()
    for location in entry.get("locations", []):
        session.add(
            SearchLocation(
                search_id=search.id,
                role=LocationRole(location["role"]),
                airport_code=location.get("airport_code"),
                country_code=location.get("country_code"),
                airport_codes=location.get("airport_codes"),
            )
        )
    revision = SearchRevision(search_id=search.id, revision_no=1, snapshot=entry["filters"])
    session.add(revision)
    session.flush()
    result.searches_created += 1
    return search, revision.id


def _search_identity(name: str, created_at: str | None) -> tuple[str, str | None]:
    return name, created_at


def _find_existing_trip(session: Session, entry: dict) -> Trip | None:
    created_at = entry.get("created_at")
    if not created_at:
        return None
    return session.scalar(
        select(Trip).where(
            Trip.name == entry["name"], Trip.created_at == datetime.fromisoformat(created_at)
        )
    )


def _import_trips(
    session: Session,
    entries: list[dict],
    searches_by_identity: dict[tuple[str, str | None], Search],
    result: ImportResult,
) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            result.errors.append("Trip entry is not a JSON object.")
            continue
        try:
            with session.begin_nested():
                _import_trip(session, entry, searches_by_identity, result)
        except _ENTRY_ERRORS as exc:
            result.errors.append(f'Trip "{entry.get("name", "?")}": {_describe(exc)}')


def _import_trip(
    session: Session,
    entry: dict,
    searches_by_identity: dict[tuple[str, str | None], Search],
    result: ImportResult,
) -> None:
    if _find_existing_trip(session, entry) is not None:
        result.trips_matched += 1
        return
    legs = entry["legs"]
    if not 2 <= len(legs) <= 8:
        raise ValueError("A trip must contain between 2 and 8 legs.")
    resolved_legs = []
    for leg in legs:
        reference = leg["search"]
        identity = _search_identity(reference["name"], reference.get("created_at"))
        search = searches_by_identity.get(identity)
        if search is None:
            search = _find_existing_search(session, reference)
        if search is None:
            raise ValueError(f'Trip leg search "{reference["name"]}" is missing.')
        resolved_legs.append((leg, search))

    search_ids = [search.id for _, search in resolved_legs]
    existing_leg = session.scalar(
        select(TripLeg.search_id).where(TripLeg.search_id.in_(search_ids)).limit(1)
    )
    if existing_leg is not None:
        raise ValueError("A Trip leg search is already assigned to another Trip.")

    trip = Trip(
        name=entry["name"],
        starts_on=date.fromisoformat(entry["starts_on"]),
        ends_on=date.fromisoformat(entry["ends_on"]),
        status=SearchStatus(entry.get("status", SearchStatus.ACTIVE.value)),
        poll_interval_minutes=int(entry.get("poll_interval_minutes", 1440)),
        # Without a stored time an active Trip would never be scheduled again.
        next_poll_at=datetime.fromisoformat(entry["next_poll_at"])
        if entry.get("next_poll_at")
        else datetime.now(UTC),
        last_polled_at=datetime.fromisoformat(entry["last_polled_at"])
        if entry.get("last_polled_at")
        else None,
        archived_at=datetime.fromisoformat(entry["archived_at"])
        if entry.get("archived_at")
        else None,
    )
    if entry.get("created_at"):
        trip.created_at = datetime.fromisoformat(entry["created_at"])
    if entry.get("updated_at"):
        trip.updated_at = datetime.fromisoformat(entry["updated_at"])
    session.add(trip)
    session.flush()
    for leg, search in resolved_legs:
        search.next_poll_at = None  # legs are polled through their Trip only
        session.add(
            TripLeg(
                trip_id=trip.id,
                search_id=search.id,
                position=int(leg["position"]),
                min_layover_days=leg.get("min_layover_days"),
                max_layover_days=leg.get("max_layover_days"),
            )
        )
    session.flush()
    result.trips_created += 1


# Untrusted file content: anything malformed becomes an error message, never a server error.
_ENTRY_ERRORS = (KeyError, TypeError, ValueError, AttributeError, SQLAlchemyError)


def _describe(exc: Exception) -> str:
    if isinstance(exc, SQLAlchemyError):
        return "rejected by the database (unknown airport or country, or a value out of range)"
    if isinstance(exc, ValidationError):
        return "invalid filters: " + "; ".join(error["msg"] for error in exc.errors())
    return str(exc)


def _price(raw: object) -> Decimal:
    try:
        price = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"Invalid price: {raw!r}") from None
    if not price.is_finite() or price < 0:
        raise ValueError(f"Invalid price: {raw!r}")
    return price


def _price_key(raw: dict) -> tuple:
    return (
        raw["origin_iata"],
        raw["destination_iata"],
        raw["cabin_class"],
        raw["departure_date"],
        raw.get("return_date"),
        raw.get("stops"),
        raw["provider"],
        raw["source"],
        raw["observed_at"],
    )


def _existing_price_key(price: PriceHistory) -> tuple:
    return (
        price.origin_iata,
        price.destination_iata,
        price.cabin_class.value,
        _iso(price.departure_date),
        _iso(price.return_date),
        price.stops,
        price.provider,
        price.source.value,
        _iso(price.observed_at),
    )


def _log_key(raw: dict) -> tuple:
    return (
        raw["origin_iata"],
        raw["destination_iata"],
        raw["cabin_class"],
        raw["departure_month"],
        raw["started_at"],
    )


def _existing_log_key(entry: QueryLog) -> tuple:
    return (
        entry.origin_iata,
        entry.destination_iata,
        entry.cabin_class.value,
        _iso(entry.departure_month),
        _iso(entry.started_at),
    )


def _import_prices(
    session: Session, search: Search, revision_id: int, entry: dict, result: ImportResult
) -> None:
    seen = {
        _existing_price_key(price)
        for price in session.scalars(
            select(PriceHistory).where(PriceHistory.search_id == search.id)
        )
    }
    for raw in entry.get("price_history", []):
        key = _price_key(raw)
        if key in seen:
            continue
        session.add(
            PriceHistory(
                search_id=search.id,
                search_revision_id=revision_id,
                source=PriceSource(raw["source"]),
                provider=raw["provider"],
                origin_iata=raw["origin_iata"],
                destination_iata=raw["destination_iata"],
                departure_date=date.fromisoformat(raw["departure_date"]),
                return_date=date.fromisoformat(raw["return_date"])
                if raw.get("return_date")
                else None,
                cabin_class=CabinClass(raw["cabin_class"]),
                stops=raw.get("stops"),
                price=_price(raw["price"]),
                currency=raw["currency"],
                airline=raw.get("airline"),
                details=raw.get("details"),
                observed_at=datetime.fromisoformat(raw["observed_at"]),
            )
        )
        seen.add(key)
        result.prices_added += 1


def _import_logs(session: Session, search: Search, entry: dict, result: ImportResult) -> None:
    seen = {
        _existing_log_key(log_entry)
        for log_entry in session.scalars(select(QueryLog).where(QueryLog.search_id == search.id))
    }
    for raw in entry.get("query_log", []):
        key = _log_key(raw)
        if key in seen:
            continue
        session.add(
            QueryLog(
                search_id=search.id,
                search_name=entry["name"],
                provider=raw["provider"],
                origin_iata=raw["origin_iata"],
                destination_iata=raw["destination_iata"],
                departure_month=date.fromisoformat(raw["departure_month"]),
                cabin_class=CabinClass(raw["cabin_class"]),
                outcome=QueryOutcome(raw["outcome"]),
                quotes_found=raw.get("quotes_found", 0),
                quotes_stored=raw.get("quotes_stored", 0),
                error=raw.get("error"),
                results=raw.get("results"),
                started_at=datetime.fromisoformat(raw["started_at"]),
                duration_ms=raw.get("duration_ms", 0),
            )
        )
        seen.add(key)
        result.logs_added += 1


def import_payload(session: Session, payload: dict) -> ImportResult:
    """Adds every new Suchabo/price/log entry from `payload`; never overwrites or deletes."""
    result = ImportResult()
    if not isinstance(payload, dict) or payload.get("format") not in SUPPORTED_FORMATS:
        result.errors.append("Unrecognised export file (wrong or missing format version).")
        return result
    searches, trips = payload.get("searches", []), payload.get("trips", [])
    if not isinstance(searches, list) or not isinstance(trips, list):
        result.errors.append("Unrecognised export file (searches and trips must be lists).")
        return result
    searches_by_identity: dict[tuple[str, str | None], Search] = {}
    for entry in searches:
        if not isinstance(entry, dict):
            result.errors.append("Tracked search entry is not a JSON object.")
            continue
        try:
            # A savepoint per entry keeps the session usable after a database error, so every
            # broken entry is reported (the caller rolls the whole import back on any error).
            with session.begin_nested():
                search, revision_id = _get_or_create_search(session, entry, result)
                _import_prices(session, search, revision_id, entry, result)
                _import_logs(session, search, entry, result)
                session.flush()
        except _ENTRY_ERRORS as exc:
            result.errors.append(f'"{entry.get("name", "?")}": {_describe(exc)}')
            continue
        searches_by_identity[_search_identity(entry["name"], entry.get("created_at"))] = search
    if payload.get("format") >= 2:
        _import_trips(session, trips, searches_by_identity, result)
    return result
