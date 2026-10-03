"""Trip Planner use cases built on top of ordinary one-way tracked searches."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session, selectinload

from flighttracker.domain.filters import CabinClass, SearchFilters, TripType
from flighttracker.domain.locations import LocationRef, expand_to_airports
from flighttracker.domain.spec import SearchSpec, trip_requests_per_poll
from flighttracker.domain.trips import (
    LayoverRule,
    TripItinerary,
    TripLegQuote,
    arrival_at,
    connection_fits,
    find_itineraries,
)
from flighttracker.models import (
    FetchJob,
    PriceHistory,
    PriceSource,
    QueryLog,
    QueryOutcome,
    Search,
    SearchRevision,
    Trip,
    TripLeg,
)
from flighttracker.services import history
from flighttracker.services.searches import SearchInput, create_search

MAX_TRIP_WINDOW_DAYS = 60
MAX_TRIP_LEGS = 8
MAX_TRIP_NAME_LENGTH = 100


@dataclass(frozen=True)
class TripLegInput:
    origin: str
    destination: str
    min_layover_days: int | None = None
    max_layover_days: int | None = None
    country_airports: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class TripInput:
    name: str
    starts_on: date
    ends_on: date
    poll_interval_minutes: int
    legs: tuple[TripLegInput, ...]
    cabin_classes: frozenset[CabinClass]
    max_stops: int | None
    adults: int
    currency: str
    children: int = 0


class TripValidationError(ValueError):
    pass


def _location(code: str) -> LocationRef:
    return LocationRef.country(code) if len(code) == 2 else LocationRef.airport(code)


def _endpoint_airports(code: str, selections: dict[str, frozenset[str]]) -> set[str]:
    return set(expand_to_airports({_location(code)}, selections))


def validate_trip(data: TripInput, today: date) -> None:
    # Leg searches are named "<trip> · 8 ZRH–BCN" and must still fit `searches.name` (120).
    if not data.name.strip() or len(data.name.strip()) > MAX_TRIP_NAME_LENGTH:
        raise TripValidationError("Enter a trip name of at most 100 characters.")
    if data.starts_on < today:
        raise TripValidationError("The trip search window must start today or later.")
    if data.ends_on < data.starts_on:
        raise TripValidationError("The end date must be on or after the start date.")
    if (data.ends_on - data.starts_on).days + 1 > MAX_TRIP_WINDOW_DAYS:
        raise TripValidationError("The trip window cannot exceed 60 days.")
    if data.poll_interval_minutes < 15:
        raise TripValidationError("Trip polls must be at least 15 minutes apart.")
    if data.poll_interval_minutes > 43200:
        raise TripValidationError("Trip polls cannot be more than 30 days apart.")
    if data.max_stops is not None and not 0 <= data.max_stops <= 3:
        raise TripValidationError("Maximum stops per leg must be between 0 and 3.")
    if not 2 <= len(data.legs) <= MAX_TRIP_LEGS:
        raise TripValidationError("A trip must contain between 2 and 8 legs.")
    if not data.cabin_classes:
        raise TripValidationError("Select at least one cabin class.")
    if not 1 <= data.adults <= 9:
        raise TripValidationError("Choose between 1 and 9 adults.")
    if not 0 <= data.children <= 8:
        raise TripValidationError("Choose between 0 and 8 children.")
    if data.adults + data.children > 9:
        raise TripValidationError("At most 9 passengers in total.")
    if len(data.currency) != 3 or not data.currency.isalpha() or not data.currency.isupper():
        raise TripValidationError("Enter a three-letter uppercase currency code.")
    for index, leg in enumerate(data.legs):
        if len(leg.origin) not in {2, 3} or len(leg.destination) not in {2, 3}:
            raise TripValidationError(
                "Use airport codes or two-letter country codes for every leg."
            )
        if leg.origin == leg.destination:
            raise TripValidationError("Origin and destination must differ for every leg.")
        if index:
            previous = data.legs[index - 1]
            previous_airports = _endpoint_airports(previous.destination, previous.country_airports)
            origin_airports = _endpoint_airports(leg.origin, leg.country_airports)
            if previous_airports and origin_airports and not previous_airports & origin_airports:
                raise TripValidationError(
                    "Every leg must start at an airport served by the previous leg."
                )
        if (
            leg.min_layover_days is not None
            and not 1 <= leg.min_layover_days <= MAX_TRIP_WINDOW_DAYS
        ):
            raise TripValidationError("Minimum layover must be between 1 and 60 days.")
        if (
            leg.max_layover_days is not None
            and not 1 <= leg.max_layover_days <= MAX_TRIP_WINDOW_DAYS
        ):
            raise TripValidationError("Maximum layover must be between 1 and 60 days.")
        if (
            leg.min_layover_days is not None
            and leg.max_layover_days is not None
            and leg.max_layover_days < leg.min_layover_days
        ):
            raise TripValidationError("Maximum layover cannot be shorter than minimum layover.")


def _leg_specs(data: TripInput) -> list[SearchSpec]:
    """The one-way Suchabo of every leg; all legs share cabin, stops and passengers."""
    filters = SearchFilters(
        trip_type=TripType.ONE_WAY,
        cabin_classes=data.cabin_classes,
        max_stops=data.max_stops,
        months_ahead=1,
        adults=data.adults,
        children=data.children,
        currency=data.currency,
        days_per_month=1,
    )
    return [
        SearchSpec(
            origins=frozenset({_location(leg.origin)}),
            destinations=frozenset({_location(leg.destination)}),
            filters=filters,
            country_airports=leg.country_airports,
        )
        for leg in data.legs
    ]


def estimated_requests(data: TripInput) -> int:
    """Upper bound of provider requests per poll (for the user quota), before saving."""
    return trip_requests_per_poll(_leg_specs(data), (data.ends_on - data.starts_on).days + 1)


def create_trip(
    session: Session, data: TripInput, *, max_route_pairs: int, owner_id: int | None = None
) -> Trip:
    """Create the Trip and its ordinary one-way Suchabos as one transaction."""
    validate_trip(data, datetime.now(UTC).date())
    now = datetime.now(UTC)
    trip = Trip(
        name=data.name.strip(),
        owner_id=owner_id,
        starts_on=data.starts_on,
        ends_on=data.ends_on,
        poll_interval_minutes=data.poll_interval_minutes,
        next_poll_at=now,
    )
    session.add(trip)
    session.flush()

    for position, (leg, spec) in enumerate(zip(data.legs, _leg_specs(data), strict=True)):
        search = create_search(
            session,
            SearchInput(
                name=f"{trip.name} · {position + 1} {leg.origin}–{leg.destination}",
                spec=spec,
                poll_interval_minutes=data.poll_interval_minutes,
            ),
            max_route_pairs=max_route_pairs,
            owner_id=owner_id,
        )
        # Trip legs are polled only by their parent Trip's coordinated job.
        search.next_poll_at = None
        session.add(
            TripLeg(
                trip_id=trip.id,
                search_id=search.id,
                position=position,
                min_layover_days=leg.min_layover_days if position else None,
                max_layover_days=leg.max_layover_days if position else None,
            )
        )
    session.flush()
    return trip


def list_trips(
    session: Session, *, archived: bool = False, visible: ColumnElement[bool] | None = None
) -> list[Trip]:
    """`visible`: condition from `services.access.visible_trips` (None = all)."""
    statement = (
        select(Trip)
        .options(selectinload(Trip.legs).selectinload(TripLeg.search))
        .order_by(Trip.created_at.desc(), Trip.id.desc())
    )
    statement = statement.where(
        Trip.archived_at.is_not(None) if archived else Trip.archived_at.is_(None)
    )
    if visible is not None:
        statement = statement.where(visible)
    return list(session.scalars(statement))


def trip_search_ids(session: Session, trips: list[Trip]) -> set[int]:
    return {leg.search_id for trip in trips for leg in trip.legs}


def _passengers(search: Search) -> tuple[int, int]:
    filters = SearchFilters.model_validate(search.filters)
    return filters.adults, filters.children


def itineraries_for_trip(
    session: Session, trip: Trip, fake_providers: tuple[str, ...] = ()
) -> list[TripItinerary]:
    points_by_leg = [
        [
            point
            for point in history.price_points(
                session, leg.search_id, fake_providers, passengers=_passengers(leg.search)
            )
            if point.return_date is None
        ]
        for leg in trip.legs
    ]
    rules = [LayoverRule(leg.min_layover_days, leg.max_layover_days) for leg in trip.legs[1:]]
    return find_itineraries(points_by_leg, rules, trip.starts_on, trip.ends_on)


def get_trip(session: Session, trip_id: int) -> Trip | None:
    return session.scalar(
        select(Trip)
        .where(Trip.id == trip_id)
        .options(selectinload(Trip.legs).selectinload(TripLeg.search))
    )


def get_trip_for_search(session: Session, search_id: int) -> Trip | None:
    return session.scalar(
        select(Trip)
        .join(TripLeg)
        .where(TripLeg.search_id == search_id)
        .options(selectinload(Trip.legs).selectinload(TripLeg.search))
    )


def next_leg_dates(
    trip: Trip,
    previous_paths: list[tuple[TripLegQuote, ...]],
    following_leg: TripLeg,
    today: date,
) -> tuple[date, ...]:
    """Possible departure calendar days based on arrival plus the layover window."""
    dates: set[date] = set()
    minimum_days = following_leg.min_layover_days or 0
    for path in previous_paths:
        arrival = arrival_at(path[-1])
        if arrival is None:
            continue
        earliest_day = arrival.date() + timedelta(days=minimum_days)
        latest_day = (
            arrival.date() + timedelta(days=following_leg.max_layover_days)
            if following_leg.max_layover_days is not None
            else trip.ends_on
        )
        latest_day = min(latest_day, trip.ends_on)
        if latest_day < earliest_day:
            continue
        day = max(earliest_day, today)
        while day <= latest_day:
            dates.add(day)
            day += timedelta(days=1)
    return tuple(sorted(dates))


def connect_quotes(
    previous_paths: list[tuple[TripLegQuote, ...]],
    quotes: list[TripLegQuote],
    rule: LayoverRule,
    *,
    final_leg: bool,
    ends_on: date,
) -> tuple[list[TripLegQuote], list[tuple[TripLegQuote, ...]]]:
    """Keep only quotes that extend a feasible path, returning quotes and extended paths."""
    connected: list[TripLegQuote] = []
    paths: list[tuple[TripLegQuote, ...]] = []
    for quote in quotes:
        for path in previous_paths:
            if not connection_fits(path[-1], quote, rule):
                continue
            if final_leg:
                arrival = arrival_at(quote)
                if arrival is None or arrival.date() > ends_on:
                    continue
            if quote.currency != path[0].currency:
                continue
            if quote not in connected:
                connected.append(quote)
            extended = (*path, quote)
            if extended not in paths:
                paths.append(extended)
            if len(paths) >= 1000:
                return connected, paths
    return connected, paths


def store_trip_quotes(
    session: Session,
    *,
    job: FetchJob,
    search: Search,
    revision: SearchRevision,
    cabin_class,
    departure_month: date,
    provider_name: str,
    origin: str,
    destination: str,
    quotes: list,
    adults: int,
    children: int,
    error: str | None,
    started_at: datetime,
    duration_ms: int,
) -> QueryLog:
    """Persist a leg query under the parent Trip job, keeping the ordinary log format."""
    stored = 0
    if quotes:
        rows = [
            {
                "search_id": search.id,
                "search_revision_id": revision.id,
                "source": PriceSource.LIVE,
                "provider": quote.provider,
                "origin_iata": quote.origin,
                "destination_iata": quote.destination,
                "departure_date": quote.departure_date,
                "return_date": quote.return_date,
                "cabin_class": quote.cabin_class,
                "stops": quote.stops,
                "price": quote.price,
                "currency": quote.currency,
                "adults": adults,
                "children": children,
                "airline": quote.airline,
                "details": quote.details,
                "observed_at": quote.observed_at,
            }
            for quote in quotes
        ]
        from sqlalchemy.dialects.postgresql import insert

        stored = len(
            session.execute(
                insert(PriceHistory)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_price_history_observation")
                .returning(PriceHistory.id)
            ).all()
        )
    entry = QueryLog(
        job_id=job.id,
        search_id=search.id,
        search_name=search.name,
        provider=provider_name,
        origin_iata=origin,
        destination_iata=destination,
        departure_month=departure_month,
        cabin_class=cabin_class,
        outcome=QueryOutcome.FAILED if error else QueryOutcome.OK if quotes else QueryOutcome.EMPTY,
        quotes_found=len(quotes),
        quotes_stored=stored,
        error=error,
        results=[
            {
                "date": quote.departure_date.isoformat(),
                "return": quote.return_date.isoformat() if quote.return_date else None,
                "price": str(quote.price),
                "currency": quote.currency,
                "stops": quote.stops,
                "details": quote.details,
            }
            for quote in quotes
        ],
        started_at=started_at,
        duration_ms=duration_ms,
    )
    session.add(entry)
    session.flush()
    return entry
