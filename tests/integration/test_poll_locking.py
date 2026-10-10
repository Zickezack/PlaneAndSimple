"""The "Poll now" button and the scheduler run in different processes; never queue twice.

These tests need two real sessions on separate connections, so their rows are committed and
removed again afterwards (the usual rolled-back test transaction cannot be shared).
"""

import threading
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from flighttracker.domain.filters import SearchFilters, TripType
from flighttracker.models import FetchJob, Search, SearchStatus, Trip, TripLeg
from flighttracker.services import jobs

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
def sessions(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    created: dict[str, list[int]] = {"searches": [], "trips": []}
    yield factory, created
    with factory() as cleanup:  # cascades to jobs and trip legs
        cleanup.execute(delete(Trip).where(Trip.id.in_(created["trips"])))
        cleanup.execute(delete(Search).where(Search.id.in_(created["searches"])))
        cleanup.commit()


def _search(session, created, *, next_poll_at=NOW, trip_type=TripType.ROUND_TRIP) -> Search:
    search = Search(
        name="Locking test",
        status=SearchStatus.ACTIVE,
        filters=SearchFilters(trip_type=trip_type).to_json(),
        poll_interval_minutes=1440,
        revision_no=1,
        next_poll_at=next_poll_at,
    )
    session.add(search)
    session.flush()
    created["searches"].append(search.id)
    return search


def _open_jobs(session, **where) -> int:
    conditions = [getattr(FetchJob, key) == value for key, value in where.items()]
    return session.scalar(
        select(func.count())
        .select_from(FetchJob)
        .where(*conditions, FetchJob.status.in_(jobs.OPEN_STATUSES))
    )


def _poll_now_while_scheduler_is_queuing(factory, request, schedule) -> bool:
    """The scheduler queues first and has not committed yet when "poll now" arrives."""
    result = {}
    with factory() as worker:
        assert schedule(worker) == 1
        with factory() as web:
            thread = threading.Thread(target=lambda: result.update(queued=request(web)))
            thread.start()
            thread.join(0.5)
            assert thread.is_alive()  # waits for the scheduler's transaction
            worker.commit()
            thread.join(5)
            web.commit()
    return result["queued"]


def test_poll_now_waits_for_the_scheduler_and_does_not_queue_twice(sessions):
    factory, created = sessions
    with factory() as setup:
        search_id = _search(setup, created).id
        setup.commit()

    queued = _poll_now_while_scheduler_is_queuing(
        factory,
        lambda web: jobs.request_poll(web, web.get(Search, search_id), NOW),
        lambda worker: jobs.schedule_due_polls(worker, NOW),
    )
    assert not queued
    with factory() as check:
        assert _open_jobs(check, search_id=search_id) == 1


def test_poll_trip_now_waits_for_the_scheduler_and_does_not_queue_twice(sessions):
    factory, created = sessions
    with factory() as setup:
        trip = Trip(
            name="Locking test",
            starts_on=date(2026, 11, 6),
            ends_on=date(2026, 11, 13),
            poll_interval_minutes=1440,
            next_poll_at=NOW,
        )
        setup.add(trip)
        setup.flush()
        created["trips"].append(trip.id)
        leg = _search(setup, created, next_poll_at=None, trip_type=TripType.ONE_WAY)
        setup.add(TripLeg(trip_id=trip.id, search_id=leg.id, position=0))
        setup.commit()
        trip_id = trip.id

    queued = _poll_now_while_scheduler_is_queuing(
        factory,
        lambda web: jobs.request_trip_poll(web, web.get(Trip, trip_id), NOW),
        lambda worker: jobs.schedule_due_trip_polls(worker, NOW),
    )
    assert not queued
    with factory() as check:
        assert _open_jobs(check, trip_id=trip_id) == 1
