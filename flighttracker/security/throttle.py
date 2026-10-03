import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta


class LoginThrottle:
    """In-memory lockout after repeated failed logins per client.

    Per process only – good enough for a single web container. Replace with a shared
    store once the web service runs with several replicas.
    """

    def __init__(self, max_failures: int = 5, window: timedelta = timedelta(minutes=15)):
        self._max_failures = max_failures
        self._window = window
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        failures = self._failures[key]
        while failures and failures[0] <= now - self._window:
            failures.popleft()
        return failures

    def try_attempt(self, key: str, now: datetime) -> bool:
        """Counts an attempt as failed up front; False when the client is locked out.

        Checking and counting in one step means parallel requests cannot all pass the check
        before any failure is recorded. A successful login clears the count with `reset`.
        """
        with self._lock:
            failures = self._prune(key, now)
            if len(failures) >= self._max_failures:
                return False
            failures.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
