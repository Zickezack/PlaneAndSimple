"""When a price digest is due: 07:00 in the user's time zone, weekly on Mondays."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flighttracker.models import DigestFrequency
from flighttracker.services.digest import due_since, latest_slot

ZURICH = ZoneInfo("Europe/Zurich")
DAILY, WEEKLY = DigestFrequency.DAILY, DigestFrequency.WEEKLY


def at(*parts) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_daily_and_weekly_send_times():
    wednesday = at(2026, 10, 21, 5, 30)  # 07:30 in Zurich (summer time)
    assert latest_slot(DAILY, wednesday, ZURICH) == at(2026, 10, 21, 5)
    assert latest_slot(DAILY, at(2026, 10, 21, 4, 30), ZURICH) == at(2026, 10, 20, 5)
    assert latest_slot(WEEKLY, wednesday, ZURICH) == at(2026, 10, 19, 5)  # that Monday
    assert latest_slot(WEEKLY, at(2026, 10, 19, 4), ZURICH) == at(2026, 10, 12, 5)
    # After the change to winter time 07:00 is 06:00 UTC.
    assert latest_slot(DAILY, at(2026, 10, 26, 6, 30), ZURICH) == at(2026, 10, 26, 6)


def test_due_once_per_period():
    now = at(2026, 10, 21, 5, 30)
    user = SimpleNamespace(
        digest=DAILY, email="a@e.test", is_active=True, timezone=None, digest_at=None
    )
    assert due_since(user, now, "Europe/Zurich") == at(2026, 10, 20, 5)
    user.digest_at = at(2026, 10, 21, 5, 1)  # sent this morning
    assert due_since(user, now, "Europe/Zurich") is None
    # In New York it is 01:30: the digest of 07:00 there (11:00 UTC) went out yesterday.
    user.digest_at, user.timezone = at(2026, 10, 20, 11, 5), "America/New_York"
    assert due_since(user, now, "Europe/Zurich") is None
    assert due_since(user, at(2026, 10, 21, 11, 1), "Europe/Zurich") == at(2026, 10, 20, 11)
    user.digest = DigestFrequency.OFF
    assert due_since(user, at(2026, 10, 22, 12), "Europe/Zurich") is None
