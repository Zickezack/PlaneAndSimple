"""Per-user limits, so that one user cannot drive the provider usage up for everybody.

Two limits, both counting non-archived (also paused – they can be resumed any time) Suchabos
and Trips of one owner:
- `max_searches`: Suchabos plus Trips (a Trip counts once, not per leg);
- `max_requests`: provider requests per poll of all of them together – the actual cost, since
  one broad Suchabo can need more requests than twenty narrow ones.

Admins and Suchabos without owner have no limit. A change that does not increase the usage is
always allowed, even when the user is above a limit that an admin lowered later.
"""

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from flighttracker.config import Settings
from flighttracker.domain.spec import SearchSpec, requests_per_poll, trip_requests_per_poll
from flighttracker.i18n import Msg
from flighttracker.models import Search, Trip, TripLeg, User
from flighttracker.providers.registry import provider_class
from flighttracker.services.searches import spec_of


@dataclass(frozen=True)
class Usage:
    searches: int
    requests: int
    # None = unlimited.
    max_searches: int | None
    max_requests: int | None


def _samples_days(settings: Settings) -> bool:
    return provider_class(settings.flight_provider).samples_days


def search_requests(spec: SearchSpec, settings: Settings) -> int:
    return requests_per_poll(spec, samples_days=_samples_days(settings))


def trip_requests(trip: Trip) -> int:
    window_days = (trip.ends_on - trip.starts_on).days + 1
    return trip_requests_per_poll([spec_of(leg.search) for leg in trip.legs], window_days)


def limits(user: User | None, settings: Settings) -> tuple[int | None, int | None]:
    if user is None or user.is_admin:
        return None, None
    return (
        user.max_searches if user.max_searches is not None else settings.max_searches_per_user,
        user.max_requests if user.max_requests is not None else settings.max_requests_per_user,
    )


def usage(session: Session, user: User, settings: Settings) -> Usage:
    is_leg = exists().where(TripLeg.search_id == Search.id)
    searches = session.scalars(
        select(Search)
        .where(Search.owner_id == user.id, Search.archived_at.is_(None), ~is_leg)
        .options(selectinload(Search.locations))
    ).all()
    trips = session.scalars(
        select(Trip)
        .where(Trip.owner_id == user.id, Trip.archived_at.is_(None))
        .options(selectinload(Trip.legs).selectinload(TripLeg.search))
    ).all()
    requests = sum(search_requests(spec_of(s), settings) for s in searches)
    requests += sum(trip_requests(trip) for trip in trips)
    max_searches, max_requests = limits(user, settings)
    return Usage(len(searches) + len(trips), requests, max_searches, max_requests)


def check(
    session: Session,
    owner: User | None,
    settings: Settings,
    *,
    added_searches: int = 0,
    added_requests: int = 0,
) -> list[Msg]:
    """Errors if the change (`added_*`, may be negative) would exceed the owner's limits."""
    max_searches, max_requests = limits(owner, settings)
    if owner is None or (max_searches is None and max_requests is None):
        return []
    current = usage(session, owner, settings)
    errors: list[Msg] = []
    if added_searches > 0 and current.searches + added_searches > max_searches:
        errors.append(
            Msg(
                "Limit reached: at most {max} tracked searches and Trips per user "
                "(archive one to make room).",
                max=max_searches,
            )
        )
    if added_requests > 0 and current.requests + added_requests > max_requests:
        errors.append(
            Msg(
                "Too many provider requests: this needs {needed} more per poll, but only "
                "{left} of {max} are left. Choose fewer routes, months or departure dates.",
                needed=added_requests,
                left=max(max_requests - current.requests, 0),
                max=max_requests,
            )
        )
    return errors
