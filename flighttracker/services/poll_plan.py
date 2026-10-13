"""Poll schedule: the planned start of every active Suchabo's and Trip's poll.

The worker places new, imported and resumed ones into the largest free gap of the schedule
(`place_unplanned`, every tick); admins move them on the Poll schedule page (`set_offsets`) or
spread all of them evenly (`spread_all`). Durations are the ones of the Worker load overview
(latest poll, or an estimate). The rules themselves are pure: `domain/poll_plan.py`.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.poll_plan import (
    PollCycle,
    Slot,
    lanes,
    overlaps,
    place,
    spread,
    waits,
)
from flighttracker.models import Search, SearchStatus, Trip, TripLeg
from flighttracker.services.capacity import PollLoad, overview


def poll_cycle(settings: Settings) -> PollCycle:
    return PollCycle(settings.default_poll_interval_minutes, ZoneInfo(settings.display_timezone))


def poll_key(kind: str, item_id: int) -> str:
    """Form field / slot key of a Suchabo ("search-3") or Trip ("trip-5")."""
    return f"{kind}-{item_id}"


def _row(session: Session, key: str) -> Search | Trip | None:
    kind, _, item_id = key.partition("-")
    model = {"search": Search, "trip": Trip}.get(kind)
    return session.get(model, int(item_id)) if model and item_id.isdigit() else None


def _loads(session: Session, settings: Settings, now: datetime) -> list[PollLoad]:
    """Active Suchabos and Trips with their durations; starts inside the current interval (a
    start stored for a longer interval, e.g. from before an `.env` change, wraps around)."""
    interval = settings.default_poll_interval_minutes
    return [
        replace(item, offset_minutes=item.offset_minutes % interval)
        if item.offset_minutes is not None
        else item
        for item in overview(session, settings, now).items
    ]


def _slot(item: PollLoad) -> Slot:
    return Slot(poll_key(item.kind, item.id), item.offset_minutes or 0, item.seconds / 60)


def replan(row: Search | Trip, cycle: PollCycle, now: datetime) -> None:
    """Move a waiting row's next poll to its (new) planned start. A row that is due anyway
    (new, broadened, resumed) keeps it; the scheduler plans the following one."""
    if row.poll_offset_minutes is None or row.next_poll_at is None or row.next_poll_at <= now:
        return
    after = now
    if row.last_polled_at is not None:
        after = max(now, row.last_polled_at + timedelta(minutes=cycle.interval_minutes / 2))
    row.next_poll_at = cycle.next_start(row.poll_offset_minutes, after)


def _has_unplanned(session: Session, now: datetime) -> bool:
    is_leg = exists().where(TripLeg.search_id == Search.id)
    searches = exists().where(
        Search.poll_offset_minutes.is_(None),
        Search.status == SearchStatus.ACTIVE,
        Search.archived_at.is_(None),
        ~is_leg,
    )
    trips = exists().where(
        Trip.poll_offset_minutes.is_(None),
        Trip.status == SearchStatus.ACTIVE,
        Trip.archived_at.is_(None),
        Trip.ends_on > now.date(),
    )
    return bool(session.scalar(select(searches | trips)))


def place_unplanned(session: Session, settings: Settings, now: datetime) -> int:
    """Gives every active Suchabo and Trip without a planned start one, oldest first."""
    if not _has_unplanned(session, now):
        return 0
    cycle = poll_cycle(settings)
    items = _loads(session, settings, now)
    slots = [_slot(item) for item in items if item.offset_minutes is not None]
    unplanned = sorted(
        (item for item in items if item.offset_minutes is None), key=lambda i: (i.kind, i.id)
    )
    for item in unplanned:
        key = poll_key(item.kind, item.id)
        offset = place(slots, item.seconds / 60, cycle.interval_minutes)
        slots.append(Slot(key, offset, item.seconds / 60))
        row = _row(session, key)
        row.poll_offset_minutes = offset
        replan(row, cycle, now)
    return len(unplanned)


def fit_returning(session: Session, settings: Settings, now: datetime, key: str) -> bool:
    """A Suchabo or Trip back in the plan (resumed, restored) keeps its start unless it would
    run at the same time as another poll; then it moves to the largest free gap. True if moved.
    """
    session.flush()
    row = _row(session, key)
    if row is None or row.poll_offset_minutes is None:
        return False  # not placed yet: the worker places it
    interval = settings.default_poll_interval_minutes
    items = _loads(session, settings, now)
    own = next((item for item in items if poll_key(item.kind, item.id) == key), None)
    if own is None:
        return False  # still paused or archived
    others = [
        _slot(item)
        for item in items
        if item.offset_minutes is not None and poll_key(item.kind, item.id) != key
    ]
    if not overlaps(_slot(own), others, interval):
        return False
    row.poll_offset_minutes = place(others, own.seconds / 60, interval)
    replan(row, poll_cycle(settings), now)
    return True


def set_offsets(
    session: Session, settings: Settings, now: datetime, offsets: dict[str, int]
) -> int:
    """Stores changed planned starts (key → minutes into the interval); returns how many."""
    cycle = poll_cycle(settings)
    changed = 0
    for key, offset in offsets.items():
        row = _row(session, key)
        if row is None or not 0 <= offset < cycle.interval_minutes:
            continue
        if row.poll_offset_minutes == offset:
            continue
        row.poll_offset_minutes = offset
        replan(row, cycle, now)
        changed += 1
    return changed


def spread_all(session: Session, settings: Settings, now: datetime) -> int:
    """Keeps the order, but gives every poll the same free time before the next one."""
    items = _loads(session, settings, now)
    slots = [
        _slot(item)
        if item.offset_minutes is not None
        # Not placed yet: at the end of the order.
        else Slot(
            poll_key(item.kind, item.id), settings.default_poll_interval_minutes, item.seconds / 60
        )
        for item in items
    ]
    return set_offsets(
        session, settings, now, spread(slots, settings.default_poll_interval_minutes)
    )


def rescale(session: Session, old_interval: int, settings: Settings, now: datetime) -> None:
    """After the poll interval changed: every planned start keeps its relative position."""
    cycle = poll_cycle(settings)
    for model in (Search, Trip):
        rows = session.scalars(select(model).where(model.poll_offset_minutes.is_not(None)))
        for row in rows:
            scaled = round(row.poll_offset_minutes * cycle.interval_minutes / old_interval)
            row.poll_offset_minutes = scaled % cycle.interval_minutes
            replan(row, cycle, now)


@dataclass(frozen=True)
class PlannedPoll:
    load: PollLoad
    key: str
    lane: int
    # Minutes it waits for the poll before it to finish (0 = starts as planned).
    wait_minutes: float

    @property
    def offset(self) -> int | None:
        return self.load.offset_minutes

    @property
    def minutes(self) -> float:
        return self.load.seconds / 60


@dataclass(frozen=True)
class Schedule:
    cycle: PollCycle
    polls: list[PlannedPoll]
    now_minutes: float  # where in the cycle "now" is

    @property
    def lanes(self) -> int:
        return max((poll.lane for poll in self.polls), default=0) + 1


def schedule(session: Session, settings: Settings, now: datetime) -> Schedule:
    """Active Suchabos and Trips in the order of their planned start; not placed ones last."""
    cycle = poll_cycle(settings)
    items = _loads(session, settings, now)
    planned = [item for item in items if item.offset_minutes is not None]
    slots = [_slot(item) for item in planned]
    late = waits(slots, cycle.interval_minutes)
    rows = lanes(slots, cycle.interval_minutes)
    polls = [
        PlannedPoll(item, key, rows.get(key, 0), late.get(key, 0))
        for item in sorted(items, key=lambda i: (i.offset_minutes is None, i.offset_minutes or 0))
        for key in [poll_key(item.kind, item.id)]
    ]
    return Schedule(cycle, polls, cycle.position(now))
