from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from flighttracker.config import Settings
from flighttracker.security.passwords import hash_password, verify_password
from flighttracker.security.throttle import LoginThrottle

FAST = {"n": 2**10}


def test_hash_roundtrip():
    encoded = hash_password("correct horse battery", **FAST)
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong", encoded)


def test_hash_is_salted_and_compose_safe():
    first, second = hash_password("pw", **FAST), hash_password("pw", **FAST)
    assert first != second
    assert "$" not in first
    assert first.startswith("scrypt:1024:8:1:")


def test_verify_rejects_malformed_hashes():
    assert not verify_password("pw", "")
    assert not verify_password("pw", "bcrypt:abc")
    assert not verify_password("pw", "scrypt:x:8:1:salt:hash")


def test_throttle_locks_after_max_failures_and_expires():
    throttle = LoginThrottle(max_failures=3, window=timedelta(minutes=15))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(3):
        assert throttle.try_attempt("1.2.3.4", now)
    assert not throttle.try_attempt("1.2.3.4", now)
    assert throttle.try_attempt("5.6.7.8", now)
    assert throttle.try_attempt("1.2.3.4", now + timedelta(minutes=16))


def test_throttle_reset():
    throttle = LoginThrottle(max_failures=1)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert throttle.try_attempt("ip", now)
    throttle.reset("ip")
    assert throttle.try_attempt("ip", now)


def test_throttle_counts_parallel_attempts():
    throttle = LoginThrottle(max_failures=5)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=20) as pool:
        allowed = list(pool.map(lambda _: throttle.try_attempt("ip", now), range(40)))
    assert allowed.count(True) == 5


def test_settings_reject_the_example_secret_key():
    with pytest.raises(ValidationError, match="example placeholder"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://u:p@db/x",
            secret_key="change-me-to-a-long-random-value-min-32-chars",
            admin_username="admin",
            admin_password_hash="scrypt:x",
        )
