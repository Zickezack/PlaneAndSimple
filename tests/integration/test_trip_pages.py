"""Pause, resume, archive, restore and delete a Trip through the web UI, with access rights."""

import pytest
from sqlalchemy import select

from flighttracker.models import Search, SearchStatus, Trip
from flighttracker.services.access import share_with
from flighttracker.services.trips import create_trip
from tests.integration.test_trips import trip_input
from tests.integration.test_users import add_user, login_as
from tests.integration.test_web import make_client


@pytest.fixture
def client(db, session_factory):
    with make_client(session_factory) as test_client:
        yield test_client


def _trip(db, owner):
    trip = create_trip(db, trip_input(), max_route_pairs=50, owner_id=owner.id)
    db.commit()
    return trip


def test_owner_pauses_resumes_archives_and_restores(client, db):
    alice = add_user(db, "alice")
    trip = _trip(db, alice)
    token = login_as(client, "alice")

    def post(action):
        return client.post(f"/trips/{trip.id}/{action}", data={"csrf_token": token})

    assert "Trip paused." in post("pause").text
    db.refresh(trip)
    assert trip.status is SearchStatus.PAUSED
    assert "Trip is polled again." in post("resume").text
    assert "Trip archived" in post("archive").text
    archived = client.get("/searches?archived=true").text
    assert trip.name in archived and trip.name not in client.get("/searches").text
    assert "Trip restored." in post("restore").text
    db.refresh(trip)
    assert trip.status is SearchStatus.ACTIVE and not trip.is_archived
    assert client.post(f"/trips/{trip.id}/explode", data={"csrf_token": token}).status_code == 404


def test_editor_may_pause_but_not_archive_or_delete(client, db):
    alice = add_user(db, "alice")
    add_user(db, "bob")
    trip = _trip(db, alice)
    share_with(db, owner_id=alice.id, username="bob", can_edit=True, trip_id=trip.id)
    db.commit()
    token = login_as(client, "bob")
    assert "Trip paused." in client.post(f"/trips/{trip.id}/pause", data={"csrf_token": token}).text
    assert client.post(f"/trips/{trip.id}/archive", data={"csrf_token": token}).status_code == 403
    response = client.post(
        f"/trips/{trip.id}/delete", data={"csrf_token": token, "confirm_name": trip.name}
    )
    assert response.status_code == 403


def test_delete_needs_the_exact_name_and_removes_the_legs(client, db):
    alice = add_user(db, "alice")
    trip = _trip(db, alice)
    trip_id, name = trip.id, trip.name
    leg_ids = [leg.search_id for leg in trip.legs]
    token = login_as(client, "alice")
    wrong = client.post(f"/trips/{trip_id}/delete", data={"csrf_token": token, "confirm_name": "x"})
    assert "please enter the exact name" in wrong.text
    deleted = client.post(
        f"/trips/{trip_id}/delete", data={"csrf_token": token, "confirm_name": name}
    )
    assert "deleted permanently" in deleted.text
    db.expire_all()
    assert db.get(Trip, trip_id) is None
    assert db.scalars(select(Search).where(Search.id.in_(leg_ids))).all() == []
