"""LISTEN/NOTIFY wake-ups of the worker."""

import logging
import threading
import time

import psycopg
from sqlalchemy.engine import make_url

from flighttracker.services.jobs import (
    WAKE_CHANNEL,
)

log = logging.getLogger("flighttracker.worker")


class WakeListener:
    """Waits for `NOTIFY fetch_jobs` (sent when a Suchabo needs polling), at most `seconds`.

    Falls back to plain sleeping if the listening connection is unavailable.
    """

    def __init__(self, database_url: str, stop: threading.Event):
        # SQLAlchemy URL → libpq URL for a plain psycopg connection.
        url = make_url(database_url).set(drivername="postgresql")
        self._conninfo = url.render_as_string(hide_password=False)
        self._stop = stop
        self._connection: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection | None:
        if self._connection is None or self._connection.closed:
            try:
                self._connection = psycopg.connect(self._conninfo, autocommit=True)
                self._connection.execute(f"LISTEN {WAKE_CHANNEL}")
            except psycopg.Error:
                log.warning("Cannot listen for wake-ups, polling every tick instead")
                self._connection = None
        return self._connection

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
            connection = self._connect()
            if connection is None:
                self._stop.wait(remaining)
                return
            try:
                # Short slices so that a stop request is noticed within a second.
                if list(connection.notifies(timeout=min(1.0, remaining), stop_after=1)):
                    return
            except psycopg.Error:
                self._connection = None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
