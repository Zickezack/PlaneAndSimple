"""Price digests (services/digest.py): every tick the worker sends the ones due – each user on
their own, so one failure never stops the others or the polls."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from sqlalchemy.orm import Session, sessionmaker

from flighttracker.config import Settings
from flighttracker.services import digest
from flighttracker.services.settings import effective_settings
from flighttracker.worker.after_poll import step

log = logging.getLogger("flighttracker.worker")


def send_due_digests(
    session_factory: sessionmaker[Session],
    base_settings: Settings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    with session_factory() as session:
        settings = effective_settings(session, base_settings)
        now = clock()
        sent = 0
        for user_id, since in digest.due_users(session, settings, now):
            name = f"Digest of user {user_id}"
            # Marked first: a digest that fails is not tried again every minute.
            step(session, name, partial(digest.mark_run, session, user_id, now))
            if step(session, name, partial(digest.send, session, settings, user_id, since, now)):
                sent += 1
    if sent:
        log.info("%d price digests sent", sent)
    return sent
