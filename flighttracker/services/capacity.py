"""Load of the worker: does one round of polls fit into the poll interval?

The single worker runs one poll after the other. If all active Suchabos and Trips together need
longer than the platform-wide poll interval, polls back up and run later than planned. Durations
come from each one's latest completed poll; those never polled yet are extrapolated from the
measured time per (estimated) request, or from the configured pause without measurements.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, selectinload

from flighttracker.config import Settings
from flighttracker.models import (
    FetchJob,
    JobStatus,
    Search,
    SearchStatus,
    Trip,
    TripLeg,
    User,
)
from flighttracker.services.quota import search_requests, trip_requests
from flighttracker.services.searches import spec_of

# Rough time per Google Flights request on top of the configured pause: ~2 s for the result
# list, plus its occasional retries after an error response (4 s and more).
SECONDS_PER_REQUEST = 4.0
# Above this share of the interval a few more or longer polls can make them back up.
TIGHT = 0.8


def estimated_seconds(requests: int, settings: Settings) -> float:
    """Duration of a poll with this many provider requests, without measurements."""
    return requests * (settings.scraper_request_delay_seconds + SECONDS_PER_REQUEST)


@dataclass(frozen=True)
class PollLoad:
    """One active Suchabo or Trip: its requests per poll and how long its poll takes."""

    kind: str  # "search" or "trip"
    id: int
    name: str
    owner: str | None
    requests: int
    # Duration of the latest completed poll; None if it was never polled.
    measured_seconds: float | None
    seconds: float
    # Planned start in the poll schedule (services/poll_plan.py); None until placed.
    offset_minutes: int | None = None

    @property
    def url(self) -> str:
        return f"/{'searches' if self.kind == 'search' else 'trips'}/{self.id}"


@dataclass(frozen=True)
class Capacity:
    interval_seconds: float
    items: list[PollLoad]
    # Measured from the latest polls (None: nothing measured yet, estimates only).
    measured_seconds_per_request: float | None
    seconds_per_request: float
    queued: int
    # Run time of the poll waiting longest (now − its planned start).
    longest_wait_seconds: float

    @property
    def requests(self) -> int:
        return sum(item.requests for item in self.items)

    @property
    def round_seconds(self) -> float:
        return sum(item.seconds for item in self.items)

    @property
    def utilization(self) -> float:
        return self.round_seconds / self.interval_seconds

    @property
    def spare_seconds(self) -> float:
        return max(self.interval_seconds - self.round_seconds, 0)

    @property
    def spare_requests(self) -> int:
        return int(self.spare_seconds // self.seconds_per_request)

    @property
    def level(self) -> str:
        if self.utilization > 1:
            return "overloaded"
        return "tight" if self.utilization >= TIGHT else "ok"


def _latest_durations(session: Session) -> tuple[dict[int, float], dict[int, float]]:
    """Seconds of the latest completed poll per Suchabo (without Trips) and per Trip."""
    latest = (
        select(func.max(FetchJob.id))
        .where(FetchJob.status == JobStatus.DONE, FetchJob.started_at.is_not(None))
        .group_by(FetchJob.search_id, FetchJob.trip_id)
    )
    rows = session.execute(
        select(
            FetchJob.search_id, FetchJob.trip_id, FetchJob.started_at, FetchJob.finished_at
        ).where(FetchJob.id.in_(latest), FetchJob.finished_at.is_not(None))
    )
    searches: dict[int, float] = {}
    trips: dict[int, float] = {}
    for search_id, trip_id, started, finished in rows:
        seconds = max((finished - started).total_seconds(), 0)
        if trip_id is None:
            searches[search_id] = seconds
        else:
            trips[trip_id] = seconds
    return searches, trips


def overview(session: Session, settings: Settings, now: datetime) -> Capacity:
    is_leg = exists().where(TripLeg.search_id == Search.id)
    searches = session.scalars(
        select(Search)
        .where(Search.status == SearchStatus.ACTIVE, Search.archived_at.is_(None), ~is_leg)
        .options(selectinload(Search.locations))
    ).all()
    trips = session.scalars(
        select(Trip)
        .where(
            Trip.status == SearchStatus.ACTIVE,
            Trip.archived_at.is_(None),
            Trip.ends_on > now.date(),
        )
        .options(selectinload(Trip.legs).selectinload(TripLeg.search))
    ).all()
    owners = dict(session.execute(select(User.id, User.username)).all())
    search_seconds, trip_seconds = _latest_durations(session)

    raw = [
        (
            "search",
            s.id,
            s.name,
            s.owner_id,
            search_requests(spec_of(s), settings),
            search_seconds.get(s.id),
            s.poll_offset_minutes,
        )
        for s in searches
    ] + [
        (
            "trip",
            t.id,
            t.name,
            t.owner_id,
            trip_requests(t),
            trip_seconds.get(t.id),
            t.poll_offset_minutes,
        )
        for t in trips
    ]
    measured = [(requests, seconds) for *_, requests, seconds, _ in raw if seconds is not None]
    measured_requests = sum(requests for requests, _ in measured)
    per_request = (
        sum(seconds for _, seconds in measured) / measured_requests if measured_requests else None
    )
    fallback = estimated_seconds(1, settings)
    items = [
        PollLoad(
            kind,
            item_id,
            name,
            owners.get(owner_id),
            requests,
            seconds,
            seconds if seconds is not None else requests * (per_request or fallback),
            offset,
        )
        for kind, item_id, name, owner_id, requests, seconds, offset in raw
    ]

    waiting = select(FetchJob).where(FetchJob.status == JobStatus.QUEUED, FetchJob.run_after <= now)
    queued = session.scalar(select(func.count()).select_from(waiting.subquery())) or 0
    oldest = session.scalar(
        select(func.min(FetchJob.run_after)).where(
            FetchJob.status == JobStatus.QUEUED, FetchJob.run_after <= now
        )
    )
    return Capacity(
        interval_seconds=settings.default_poll_interval_minutes * 60,
        items=sorted(items, key=lambda i: -i.seconds),
        measured_seconds_per_request=per_request,
        seconds_per_request=per_request or fallback,
        queued=queued,
        longest_wait_seconds=(now - oldest).total_seconds() if oldest else 0,
    )
