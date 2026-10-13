"""Poll schedule: when within the poll interval each Suchabo and Trip starts its poll.

Every active Suchabo and Trip has a fixed start offset (minutes) inside a cycle of the
platform-wide poll interval. Cycles are counted in local wall-clock time from a fixed Monday
midnight, so with the default 24 h interval an offset is simply a time of day – also across
daylight-saving changes. The single worker runs one poll after the other: a poll whose start
comes while another one is running waits until that one is done, it never runs alongside.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DAY_MINUTES = 1440
# Start of cycle 0, naive local time (a Monday, so a weekly interval starts on Mondays).
ANCHOR = datetime(2024, 1, 1)


@dataclass(frozen=True)
class PollCycle:
    interval_minutes: int
    timezone: ZoneInfo

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.timezone).replace(tzinfo=None)

    def position(self, moment: datetime) -> float:
        """Minutes since the start of the cycle `moment` lies in."""
        elapsed = (self._local(moment) - ANCHOR) / timedelta(minutes=1)
        return elapsed % self.interval_minutes

    def next_start(self, offset_minutes: int, after: datetime, *, inclusive=False) -> datetime:
        """First planned start of this offset after `after` (UTC); `inclusive`: or at it."""
        interval = self.interval_minutes
        offset = offset_minutes % interval
        elapsed = (self._local(after) - ANCHOR) / timedelta(minutes=1)
        # One cycle early: an hour repeated by the DST change can map back before `after`.
        cycle = math.floor((elapsed - offset) / interval)
        while True:
            local = ANCHOR + timedelta(minutes=cycle * interval + offset)
            start = local.replace(tzinfo=self.timezone).astimezone(UTC)
            if start > after or (inclusive and start == after):
                return start
            cycle += 1

    def next_poll(self, offset_minutes: int, polled_at: datetime) -> datetime:
        """Next planned start after a poll queued at `polled_at`: at least half an interval
        later, so a poll off the plan (new, broadened, "poll now") is not followed by the
        planned one minutes later."""
        half = timedelta(minutes=self.interval_minutes / 2)
        return self.next_start(offset_minutes, polled_at + half, inclusive=True)


@dataclass(frozen=True)
class Slot:
    """A planned poll: `key` identifies the Suchabo or Trip, `minutes` its expected duration."""

    key: str
    start: int
    minutes: float


def _gaps(slots: list[Slot], interval: int) -> list[tuple[float, float]]:
    """(begin, length) of the free time after each slot up to the next start, cyclic."""
    ordered = sorted(slots, key=lambda slot: slot.start)
    gaps = []
    for index, slot in enumerate(ordered):
        following = ordered[(index + 1) % len(ordered)].start
        if index + 1 == len(ordered):
            following += interval
        end = slot.start + slot.minutes
        gaps.append((end, following - end))
    return gaps


def place(slots: list[Slot], minutes: float, interval: int) -> int:
    """Start for a new poll: centred in the largest free gap, as far as possible from the
    neighbours on both sides. An empty plan starts at the beginning of the cycle."""
    if not slots:
        return 0
    begin, length = max(_gaps(slots, interval), key=lambda gap: gap[1])
    start = begin + max(length - minutes, 0) / 2
    return round(start) % interval


def spread(slots: list[Slot], interval: int, first_start: int = 0) -> dict[str, int]:
    """Same order, equal free time between all polls; back to back if they do not fit."""
    if not slots:
        return {}
    ordered = sorted(slots, key=lambda slot: (slot.start, slot.key))
    spare = max(interval - sum(slot.minutes for slot in ordered), 0) / len(ordered)
    starts = {}
    moment = float(first_start)
    for slot in ordered:
        starts[slot.key] = round(moment) % interval
        moment += slot.minutes + spare
    return starts


def waits(slots: list[Slot], interval: int) -> dict[str, float]:
    """Minutes each poll waits for the one before it to finish (0 = starts as planned).

    Simulates the single worker over two cycles, so a poll running past the end of a cycle
    delays the first ones of the next. With more work than the interval holds, the delays
    keep growing in reality – this then shows the lower bound.
    """
    ordered = sorted(slots, key=lambda slot: slot.start)
    free_at = -math.inf
    result: dict[str, float] = {}
    for lap in (0, 1):
        for slot in ordered:
            planned = slot.start + lap * interval
            begin = max(planned, free_at)
            result[slot.key] = begin - planned
            free_at = begin + slot.minutes
    return result


def _segments(slot: Slot, interval: int) -> list[tuple[float, float]]:
    """(begin, end) on the timeline; a poll running past the end of the interval wraps."""
    end = slot.start + slot.minutes
    if end <= interval:
        return [(slot.start, end)]
    return [(slot.start, interval), (0, end - interval)]


def overlaps(slot: Slot, others: list[Slot], interval: int) -> bool:
    """Whether the poll would run at the same time as one of the others (cyclic)."""
    own = _segments(slot, interval)
    return any(
        b < end and begin < e
        for other in others
        for begin, end in _segments(other, interval)
        for b, e in own
    )


def lanes(slots: list[Slot], interval: int) -> dict[str, int]:
    """Row of each slot on the timeline, so that overlapping polls are drawn below each other."""
    taken: list[list[tuple[float, float]]] = []
    result = {}
    for slot in sorted(slots, key=lambda slot: slot.start):
        segments = _segments(slot, interval)
        lane = next(
            (
                index
                for index, row in enumerate(taken)
                if all(e <= begin or end <= b for b, e in segments for begin, end in row)
            ),
            len(taken),
        )
        if lane == len(taken):
            taken.append([])
        taken[lane].extend(segments)
        result[slot.key] = lane
    return result


def offset_parts(offset_minutes: int, interval: int) -> tuple[int | None, str]:
    """(day, "HH:MM"): the day (1-based) only for intervals longer than a day."""
    day, minute_of_day = divmod(offset_minutes, DAY_MINUTES)
    clock = f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
    return (day + 1 if interval > DAY_MINUTES else None), clock


def parse_offset(day: int, clock: str, interval: int) -> int:
    """Offset from a day (1-based) and "HH:MM"; raises ValueError outside the interval."""
    hours, _, minutes = clock.strip().partition(":")
    hour, minute = int(hours), int(minutes)
    if not (0 <= hour < 24 and 0 <= minute < 60 and day >= 1):
        raise ValueError(clock)
    offset = (day - 1) * DAY_MINUTES + hour * 60 + minute
    if offset >= interval:
        raise ValueError(clock)
    return offset
