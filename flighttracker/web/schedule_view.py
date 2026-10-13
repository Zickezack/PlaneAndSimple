"""Poll schedule timeline for the templates: geometry of the SVG bars (`settings/_timeline.html`,
read-only under Worker load, draggable on the edit page via static/js/poll-schedule.js)."""

import math

from flighttracker.domain.poll_plan import DAY_MINUTES, offset_parts
from flighttracker.services.poll_plan import Schedule

# Timeline geometry in SVG units; the script redraws it in screen pixels.
WIDTH = 1000
AXIS = 22
LANE = 26
BAR = 20
MIN_BAR = 4
# Tick spacing: the first that gives at most 8 ticks per interval.
_TICK_STEPS = (15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080)


def _ticks(interval: int) -> list[dict]:
    step = next((s for s in _TICK_STEPS if interval / s <= 8), interval)
    return [
        {"x": minute / interval * WIDTH, "day": day, "clock": clock}
        for minute in range(0, interval, step)
        for day, clock in [offset_parts(minute, interval)]
    ]


def _bars(plan: Schedule) -> list[dict]:
    """One bar per placed poll; a poll running past the end of the interval wraps around."""
    interval = plan.cycle.interval_minutes
    bars = []
    for number, poll in enumerate(plan.polls, start=1):
        if poll.offset is None:
            continue
        x = poll.offset / interval * WIDTH
        width = max(poll.minutes / interval * WIDTH, MIN_BAR)
        segments = [(x, min(width, WIDTH - x))]
        if x + width > WIDTH:
            segments.append((0, x + width - WIDTH))
        bars.append(
            {"number": number, "poll": poll, "y": AXIS + poll.lane * LANE, "segments": segments}
        )
    return bars


def timeline(plan: Schedule) -> dict:
    """Template context of the timeline (and of the start fields of the edit page)."""
    interval = plan.cycle.interval_minutes

    def label(offset: int) -> str:
        return offset_parts(offset, interval)[1]

    return {
        "plan": plan,
        "interval": interval,
        "days": math.ceil(interval / DAY_MINUTES) if interval > DAY_MINUTES else 0,
        "offset_parts": lambda offset: offset_parts(offset, interval),
        "ticks": _ticks(interval),
        "bars": _bars(plan),
        "height": AXIS + plan.lanes * LANE + 4,
        "now_x": plan.now_minutes / interval * WIDTH,
        "geometry": {"width": WIDTH, "axis": AXIS, "lane": LANE, "bar": BAR},
        # For the script: the same data the bars are drawn from.
        "model": [
            {
                "key": poll.key,
                "number": number,
                "name": poll.load.name,
                "start": poll.offset,
                "minutes": poll.minutes,
                "label": label(poll.offset) if poll.offset is not None else "",
            }
            for number, poll in enumerate(plan.polls, start=1)
        ],
    }
