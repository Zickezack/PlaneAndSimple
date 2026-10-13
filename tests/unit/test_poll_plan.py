from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from flighttracker.domain.poll_plan import (
    PollCycle,
    Slot,
    lanes,
    offset_parts,
    overlaps,
    parse_offset,
    place,
    spread,
    waits,
)

ZURICH = ZoneInfo("Europe/Zurich")
DAILY = PollCycle(1440, ZURICH)


def zurich(*args) -> datetime:
    return datetime(*args, tzinfo=ZURICH).astimezone(UTC)


def test_daily_offset_is_a_local_time_of_day():
    assert DAILY.next_start(7 * 60 + 30, zurich(2026, 10, 13, 6, 0)) == zurich(2026, 10, 13, 7, 30)
    assert DAILY.next_start(7 * 60 + 30, zurich(2026, 10, 13, 7, 30)) == zurich(2026, 10, 14, 7, 30)


def test_time_of_day_survives_daylight_saving_change():
    # 25 October 2026: clocks go back from 03:00 to 02:00 in Zurich.
    start = DAILY.next_start(6 * 60, zurich(2026, 10, 25, 1, 0))
    assert start == zurich(2026, 10, 25, 6, 0)
    assert start - zurich(2026, 10, 24, 6, 0) == timedelta(hours=25)


def test_shorter_interval_repeats_within_the_day():
    cycle = PollCycle(360, ZURICH)
    assert cycle.next_start(30, zurich(2026, 10, 13, 6, 45)) == zurich(2026, 10, 13, 12, 30)
    assert cycle.position(zurich(2026, 10, 13, 13, 0)) == 60


def test_next_poll_skips_a_planned_start_shortly_after_an_extra_poll():
    created = zurich(2026, 10, 13, 6, 0)
    assert DAILY.next_poll(7 * 60, created) == zurich(2026, 10, 14, 7, 0)
    assert DAILY.next_poll(20 * 60, created) == zurich(2026, 10, 13, 20, 0)


def test_place_centres_a_new_poll_in_the_largest_gap():
    assert place([], 30, 1440) == 0
    slots = [Slot("a", 0, 60), Slot("b", 120, 60)]
    # Free 180..1440 is the largest gap; the 60-minute poll sits in its middle.
    assert place(slots, 60, 1440) == 180 + (1260 - 60) // 2


def test_place_wraps_around_midnight():
    slots = [Slot("a", 600, 60), Slot("b", 1300, 60)]
    # Gap from 1360 over midnight to 600 (680 min) beats 660..1300 (640 min).
    assert place(slots, 0, 1440) == (1360 + 340) % 1440


def test_spread_keeps_the_order_and_equal_spare_time():
    slots = [Slot("b", 500, 120), Slot("a", 10, 60), Slot("c", 900, 60)]
    assert spread(slots, 1440) == {"a": 0, "b": 60 + 400, "c": 60 + 400 + 120 + 400}


def test_spread_packs_back_to_back_when_overloaded():
    slots = [Slot("a", 0, 1000), Slot("b", 1, 1000)]
    assert spread(slots, 1440) == {"a": 0, "b": 1000}


def test_waits_for_overlapping_and_wrapping_polls():
    slots = [Slot("a", 0, 90), Slot("b", 60, 30), Slot("c", 1430, 20)]
    result = waits(slots, 1440)
    assert result["b"] == 40  # a starts 10 minutes late and runs until 100
    assert result["a"] == 10  # c runs 10 minutes past midnight
    assert result["c"] == 0


def test_lanes_stack_overlapping_polls():
    slots = [Slot("a", 0, 90), Slot("b", 60, 30), Slot("c", 100, 10)]
    assert lanes(slots, 1440) == {"a": 0, "b": 1, "c": 0}


def test_offsets_round_trip_through_the_form():
    assert offset_parts(450, 1440) == (None, "07:30")
    assert offset_parts(1440 + 450, 10080) == (2, "07:30")
    assert parse_offset(1, "07:30", 1440) == 450
    assert parse_offset(2, "07:30", 10080) == 1440 + 450
    for day, clock in [(1, "24:00"), (2, "00:00"), (1, "7"), (0, "01:00")]:
        with pytest.raises(ValueError):
            parse_offset(day, clock, 1440)


def test_lanes_keep_the_wrapped_part_of_a_poll_free():
    slots = [Slot("a", 0, 30), Slot("b", 1400, 60)]  # b runs until 00:20
    assert lanes(slots, 1440) == {"a": 0, "b": 1}


def test_overlaps_also_across_the_end_of_the_interval():
    others = [Slot("a", 0, 30), Slot("b", 600, 60)]
    assert overlaps(Slot("x", 620, 10), others, 1440)
    assert overlaps(Slot("x", 1430, 20), others, 1440)  # runs into a at 00:00
    assert not overlaps(Slot("x", 30, 570), others, 1440)  # touching is fine
