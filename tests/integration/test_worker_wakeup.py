import threading
import time

import psycopg
from sqlalchemy.engine import make_url

from flighttracker.services.jobs import WAKE_CHANNEL
from flighttracker.worker.wake import WakeListener


def test_notify_wakes_the_worker_early(engine):
    database_url = engine.url.render_as_string(hide_password=False)
    stop = threading.Event()
    listener = WakeListener(database_url, stop)
    try:
        listener.wait(0.1)  # connects and starts listening
        conninfo = make_url(database_url).set(drivername="postgresql")
        with psycopg.connect(conninfo.render_as_string(hide_password=False), autocommit=True) as c:
            c.execute(f"NOTIFY {WAKE_CHANNEL}")
        started = time.monotonic()
        listener.wait(10)
        assert time.monotonic() - started < 3
    finally:
        listener.close()


def test_stop_ends_the_wait(engine):
    stop = threading.Event()
    listener = WakeListener(engine.url.render_as_string(hide_password=False), stop)
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    listener.wait(10)
    listener.close()
    assert time.monotonic() - started < 3
