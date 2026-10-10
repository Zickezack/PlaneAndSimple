"""Export of Suchabos and Trips as JSON (global export and per-search export).

The counterpart is `data_import.py`; `FORMAT_VERSION` is the format written here.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from flighttracker.models import (
    PriceHistory,
    QueryLog,
    Search,
    Trip,
    TripLeg,
)

FORMAT_VERSION = 2


def iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _price_to_dict(price: PriceHistory) -> dict:
    return {
        "source": price.source.value,
        "provider": price.provider,
        "origin_iata": price.origin_iata,
        "destination_iata": price.destination_iata,
        "departure_date": iso(price.departure_date),
        "return_date": iso(price.return_date),
        "cabin_class": price.cabin_class.value,
        "stops": price.stops,
        "price": str(price.price),
        "currency": price.currency,
        "adults": price.adults,
        "children": price.children,
        "airline": price.airline,
        "details": price.details,
        "observed_at": iso(price.observed_at),
    }


def _log_to_dict(entry: QueryLog) -> dict:
    return {
        "provider": entry.provider,
        "origin_iata": entry.origin_iata,
        "destination_iata": entry.destination_iata,
        "departure_month": iso(entry.departure_month),
        "cabin_class": entry.cabin_class.value,
        "outcome": entry.outcome.value,
        "quotes_found": entry.quotes_found,
        "quotes_stored": entry.quotes_stored,
        "error": entry.error,
        "results": entry.results,
        "started_at": iso(entry.started_at),
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
        "created_at": iso(search.created_at),
        "archived_at": iso(search.archived_at),
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
    return {"name": search.name, "created_at": iso(search.created_at)}


def _export_trip(trip: Trip) -> dict:
    return {
        "name": trip.name,
        "starts_on": iso(trip.starts_on),
        "ends_on": iso(trip.ends_on),
        "status": trip.status.value,
        "poll_interval_minutes": trip.poll_interval_minutes,
        "next_poll_at": iso(trip.next_poll_at),
        "last_polled_at": iso(trip.last_polled_at),
        "created_at": iso(trip.created_at),
        "updated_at": iso(trip.updated_at),
        "archived_at": iso(trip.archived_at),
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
        "exported_at": iso(datetime.now(UTC)),
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
        "exported_at": iso(datetime.now(UTC)),
        "searches": [_export_one(session, search) for search in searches],
        "trips": [_export_trip(trip) for trip in trips],
    }
