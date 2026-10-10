"""User management: login, visibility, sharing, quota, cancelling polls, passenger changes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from flighttracker.config import Settings
from flighttracker.models import (
    FetchJob,
    JobStatus,
    PriceHistory,
    PriceSource,
    QueryLog,
    Search,
    SearchStatus,
    User,
    UserRole,
)
from flighttracker.providers.mock import MockProvider
from flighttracker.services import history, jobs, quota
from flighttracker.services.access import share_with
from flighttracker.services.ingestion import plan_job
from flighttracker.services.searches import current_revision_id, update_search
from flighttracker.services.trips import create_trip
from flighttracker.services.users import create_user
from flighttracker.worker.loop import process_next_job, schedule
from tests.integration.test_services import NOW, A, create, make_input
from tests.integration.test_trips import trip_input
from tests.integration.test_web import csrf, login, make_client, search_form

USER_PASSWORD = "another-long-password"


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+psycopg://unused/unused_test",
        "secret_key": "x" * 40,
        "admin_username": "admin",
        "admin_password_hash": "unused",
    }
    return Settings(_env_file=None, **(values | overrides))


def add_user(db, username, role=UserRole.USER, **fields) -> User:
    user = create_user(db, _settings(), username, USER_PASSWORD, role)
    for key, value in fields.items():
        setattr(user, key, value)
    db.commit()
    return user


def login_as(client, username, password=USER_PASSWORD) -> str:
    client.cookies.clear()
    token = csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={"csrf_token": token, "username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return csrf(client.get("/searches").text)


def own_search(db, owner: User, name="Mine", **filters) -> Search:
    search = create(db, **filters)
    search.name = name
    search.owner_id = owner.id
    db.commit()
    return search


@pytest.fixture
def client(db, session_factory):
    with make_client(session_factory) as test_client:
        yield test_client


class TestLogin:
    def test_db_user_logs_in_and_env_admin_gets_a_row(self, client, db):
        add_user(db, "alice")
        login_as(client, "alice")
        assert client.get("/searches").status_code == 200
        login(client)
        admin = db.scalars(select(User).where(User.username == "admin")).one()
        assert admin.role is UserRole.ADMIN and admin.password_hash is None

    def test_wrong_password_and_deactivated_user_are_rejected(self, client, db):
        alice = add_user(db, "alice")
        token = csrf(client.get("/login").text)
        data = {"csrf_token": token, "username": "alice", "password": "wrong-password-123"}
        assert client.post("/login", data=data).status_code == 401
        alice.is_active = False
        db.commit()
        data["password"] = USER_PASSWORD
        assert client.post("/login", data=data).status_code == 401

    def test_deactivation_ends_running_sessions(self, client, db):
        alice = add_user(db, "alice")
        login_as(client, "alice")
        alice.is_active = False
        db.commit()
        response = client.get("/searches", follow_redirects=False)
        assert response.headers["location"] == "/login"

    def test_password_change_ends_other_sessions_only(self, client, db, session_factory):
        add_user(db, "alice")
        token = login_as(client, "alice")
        with make_client(session_factory) as other:
            login_as(other, "alice")
            response = client.post(
                "/settings/password",
                data={
                    "csrf_token": token,
                    "current_password": USER_PASSWORD,
                    "new_password": "a-brand-new-password",
                    "repeat_password": "a-brand-new-password",
                },
            )
            assert "Password changed" in response.text
            assert client.get("/searches", follow_redirects=False).status_code == 200
            assert other.get("/searches", follow_redirects=False).status_code == 303


class TestVisibility:
    def test_users_only_see_their_own_and_shared_searches(self, client, db):
        alice, bob = add_user(db, "alice"), add_user(db, "bob")
        mine = own_search(db, alice, "Alice trip")
        theirs = own_search(db, bob, "Bob trip")
        unowned = own_search(db, alice, "Legacy")
        unowned.owner_id = None
        db.commit()
        login_as(client, "alice")
        page = client.get("/searches").text
        assert "Alice trip" in page and "Bob trip" not in page and "Legacy" not in page
        assert client.get(f"/searches/{mine.id}").status_code == 200
        assert client.get(f"/searches/{theirs.id}").status_code == 404
        assert client.get(f"/searches/{theirs.id}/export").status_code == 404

    def test_admin_sees_everything_with_owner_names(self, client, db):
        bob = add_user(db, "bob")
        own_search(db, bob, "Bob trip")
        login(client)
        page = client.get("/searches").text
        assert "Bob trip" in page and "bob" in page

    def test_platform_settings_and_user_management_are_admin_only(self, client, db):
        add_user(db, "alice")
        login_as(client, "alice")
        page = client.get("/settings").text
        assert "Personal settings" in page and "Platform Settings" not in page
        assert client.get("/users").status_code == 403
        assert client.get("/settings/export").status_code == 403

    def test_overlap_hints_never_reveal_other_users_searches(self, client, db):
        alice, bob = add_user(db, "alice"), add_user(db, "bob")
        own_search(db, bob, "Bob ZRH-BCN", months_ahead=6)
        token = login_as(client, "alice")
        response = client.post("/searches", data=search_form(token), follow_redirects=False)
        assert response.status_code == 303  # created, no hint about Bob's Suchabo
        assert db.scalars(select(Search).where(Search.owner_id == alice.id)).one()


class TestSharing:
    def _shared(self, client, db, permission):
        alice = add_user(db, "alice")
        add_user(db, "bob")
        search = own_search(db, alice)
        token = login_as(client, "alice")
        client.post(
            f"/searches/{search.id}/shares",
            data={"csrf_token": token, "username": "bob", "permission": permission},
        )
        return search, login_as(client, "bob")

    def test_view_share_allows_reading_only(self, client, db):
        search, token = self._shared(client, db, "view")
        assert client.get(f"/searches/{search.id}").status_code == 200
        assert client.get(f"/searches/{search.id}/edit").status_code == 403
        response = client.post(f"/searches/{search.id}/poll", data={"csrf_token": token})
        assert response.status_code == 403

    def test_edit_share_allows_editing_but_not_deleting_or_sharing(self, client, db):
        search, token = self._shared(client, db, "edit")
        assert client.get(f"/searches/{search.id}/edit").status_code == 200
        response = client.post(
            f"/searches/{search.id}/pause", data={"csrf_token": token}, follow_redirects=False
        )
        assert response.status_code == 303
        db.refresh(search)
        assert search.status is SearchStatus.PAUSED
        for action in ("archive", "delete", "shares"):
            response = client.post(
                f"/searches/{search.id}/{action}",
                data={"csrf_token": token, "confirm_name": search.name, "username": "x"},
            )
            assert response.status_code == 403

    def test_removing_the_share_removes_access(self, client, db):
        search, _ = self._shared(client, db, "view")
        token = login_as(client, "alice")
        page = client.get(f"/searches/{search.id}").text
        share_id = int(page.split("/shares/")[1].split("/delete")[0])
        client.post(f"/searches/{search.id}/shares/{share_id}/delete", data={"csrf_token": token})
        login_as(client, "bob")
        assert client.get(f"/searches/{search.id}").status_code == 404


class TestUserManagement:
    def test_admin_creates_user_who_can_log_in(self, client, db):
        token = login(client)
        client.post(
            "/users",
            data={
                "csrf_token": token,
                "username": "carol",
                "password": USER_PASSWORD,
                "role": "user",
            },
        )
        login_as(client, "carol")

    def _deactivate(self, client, user):
        token = login(client)
        client.post(f"/users/{user.id}", data={"csrf_token": token, "role": "user"})
        return token

    def test_deactivating_pauses_searches_nobody_else_may_edit(self, client, db):
        alice = add_user(db, "alice")
        add_user(db, "bob")
        private, viewed = own_search(db, alice, "Private"), own_search(db, alice, "Viewed")
        share_with(db, owner_id=alice.id, username="bob", can_edit=False, search_id=viewed.id)
        db.commit()
        self._deactivate(client, alice)
        for search in (alice, private, viewed):
            db.refresh(search)
        assert not alice.is_active
        assert private.status is viewed.status is SearchStatus.PAUSED

    def test_edited_searches_keep_running_until_the_last_editor_is_gone(self, client, db):
        alice = add_user(db, "alice")
        add_user(db, "bob")
        search = own_search(db, alice)
        share = share_with(
            db, owner_id=alice.id, username="bob", can_edit=True, search_id=search.id
        )
        db.commit()
        token = self._deactivate(client, alice)
        db.refresh(search)
        assert search.status is SearchStatus.ACTIVE
        client.post(f"/searches/{search.id}/shares/{share.id}/delete", data={"csrf_token": token})
        db.refresh(search)
        assert search.status is SearchStatus.PAUSED

    def test_deleted_users_searches_go_to_the_admin(self, client, db):
        alice = add_user(db, "alice")
        add_user(db, "bob")
        shared, private = own_search(db, alice, "Shared"), own_search(db, alice, "Private")
        share_with(db, owner_id=alice.id, username="bob", can_edit=True, search_id=shared.id)
        db.commit()
        alice_id, ids = alice.id, (shared.id, private.id)
        token = login(client)
        client.post(
            f"/users/{alice_id}/delete", data={"csrf_token": token, "confirm_name": "alice"}
        )
        db.expire_all()
        admin = db.scalars(select(User).where(User.username == "admin")).one()
        shared, private = (db.get(Search, search_id) for search_id in ids)
        assert db.get(User, alice_id) is None
        assert shared.owner_id == private.owner_id == admin.id
        assert shared.status is SearchStatus.ACTIVE  # Bob may still edit it
        assert private.status is SearchStatus.PAUSED

    def test_searches_from_before_user_management_belong_to_the_admin(self, client, db):
        legacy = create(db)
        db.commit()
        login(client)
        db.refresh(legacy)
        admin = db.scalars(select(User).where(User.username == "admin")).one()
        assert legacy.owner_id == admin.id

    def test_admin_cannot_lock_themselves_out(self, client, db):
        token = login(client)
        admin = db.scalars(select(User).where(User.username == "admin")).one()
        client.post(f"/users/{admin.id}", data={"csrf_token": token, "role": "user"})
        db.refresh(admin)
        assert admin.is_active and admin.is_admin

    def test_username_of_env_admin_is_reserved(self, db):
        with pytest.raises(ValueError, match="already taken"):
            create_user(db, _settings(), "ADMIN", USER_PASSWORD, UserRole.USER)


class TestQuota:
    def test_search_count_limit(self, client, db):
        alice = add_user(db, "alice", max_searches=1)
        own_search(db, alice)
        token = login_as(client, "alice")
        response = client.post(
            "/searches", data=search_form(token, destinations="MAD", confirm_overlap="1")
        )
        assert response.status_code == 422
        assert "Limit reached" in response.text

    def test_request_budget_counts_routes_months_and_dates(self, db):
        alice = add_user(db, "alice", max_requests=50)
        settings = _settings(flight_provider="google_flights")
        # Fares: 1 route × 3 months × 4 sampled dates × 1 stay length = 12 requests per poll;
        # price calendar: 1 route × 1 stay length × 2 requests (61 days each for 3 months) = 2.
        own_search(db, alice, months_ahead=3)
        assert quota.usage(db, alice, settings).requests == 14
        assert quota.check(db, alice, settings, added_searches=1, added_requests=36) == []
        assert quota.check(db, alice, settings, added_searches=1, added_requests=37)

    def test_admins_and_changes_that_shrink_usage_are_never_blocked(self, db):
        admin = add_user(db, "boss", role=UserRole.ADMIN)
        alice = add_user(db, "alice", max_searches=0, max_requests=0)
        settings = _settings()
        assert quota.check(db, admin, settings, added_searches=99, added_requests=10**6) == []
        assert quota.check(db, alice, settings, added_requests=-5) == []


class TestCancelPolls:
    def test_log_lists_open_polls_and_owner_cancels_queued_one(self, client, db):
        alice = add_user(db, "alice")
        search = own_search(db, alice)
        jobs.request_poll(db, search, NOW)
        db.commit()
        token = login_as(client, "alice")
        page = client.get("/log").text
        assert "Running and queued polls" in page and "/cancel" in page
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        client.post(f"/log/jobs/{job.id}/cancel", data={"csrf_token": token})
        db.refresh(job)
        assert job.status is JobStatus.CANCELLED
        assert not jobs.has_open_poll(db, search.id)

    def test_viewers_cannot_cancel(self, client, db):
        alice = add_user(db, "alice")
        add_user(db, "bob")
        search = own_search(db, alice)
        share_with(db, owner_id=alice.id, username="bob", can_edit=False, search_id=search.id)
        jobs.request_poll(db, search, NOW)
        db.commit()
        token = login_as(client, "bob")
        page = client.get("/log").text
        assert search.name in page and "/cancel" not in page
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        client.post(f"/log/jobs/{job.id}/cancel", data={"csrf_token": token})
        db.refresh(job)
        assert job.status is JobStatus.QUEUED

    def test_running_poll_stops_after_the_current_query(self, db, session_factory):
        search = create(db, months_ahead=3)
        db.commit()
        schedule(session_factory, clock=lambda: NOW)
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()

        class CancelAfterFirstQuery(MockProvider):
            def fetch_current(self, query):
                with session_factory() as other:
                    jobs.cancel_job(other, job.id, NOW)
                    other.commit()
                return super().fetch_current(query)

        assert process_next_job(
            session_factory, CancelAfterFirstQuery(clock=lambda: NOW), clock=lambda: NOW
        )
        db.refresh(job)
        assert job.status is JobStatus.CANCELLED
        logged = db.scalars(select(QueryLog).where(QueryLog.job_id == job.id)).all()
        assert len(logged) == 1  # the query in progress finished and was kept


class TestPassengerChange:
    def test_more_passengers_are_polled_and_never_shown_as_price_increase(self, db):
        search = create(db, adults=1)
        revision_id = current_revision_id(db, search)
        departure = NOW.date() + timedelta(days=30)

        def price(amount, adults, observed_at):
            return PriceHistory(
                search_id=search.id,
                search_revision_id=revision_id,
                source=PriceSource.LIVE,
                provider="mock",
                origin_iata="ZRH",
                destination_iata="BCN",
                departure_date=departure,
                cabin_class="economy",
                stops=0,
                price=Decimal(amount),
                currency="CHF",
                adults=adults,
                children=0,
                observed_at=observed_at,
            )

        db.add_all(
            [
                price("100", 1, NOW - timedelta(days=2)),
                price("110", 1, NOW - timedelta(days=1)),
                price("230", 2, NOW),
            ]
        )
        db.flush()

        (two,) = history.price_points(db, search.id, passengers=(2, 0))
        assert (two.price, two.previous_price) == (Decimal("230"), None)
        (one,) = history.price_points(db, search.id, passengers=(1, 0))
        assert (one.price, one.previous_price) == (Decimal("110"), Decimal("100"))
        assert history.other_passenger_prices(db, search.id, (2, 0), NOW.date()) == 2

    def test_edited_passengers_reach_the_next_poll_right_away(self, db):
        search = create(db, adults=1)
        search.next_poll_at = NOW + timedelta(days=1)
        changed = make_input((A("ZRH"),), (A("BCN"),), adults=2)
        update_search(db, search, changed, max_route_pairs=50, now=NOW)
        assert search.next_poll_at == NOW  # different passengers count as broadened
        jobs.schedule_due_polls(db, NOW)
        job = db.scalars(select(FetchJob).where(FetchJob.search_id == search.id)).one()
        plan = plan_job(db, job, datetime.now(UTC))
        assert plan.queries and all(q.adults == 2 for q in plan.queries)


class TestPages:
    def test_every_page_renders_for_admins_and_users(self, client, db):
        alice = add_user(db, "alice")
        search = own_search(db, alice)
        trip = create_trip(db, trip_input(), max_route_pairs=50, owner_id=alice.id)
        db.commit()
        login(client)
        for url in (
            "/users",
            f"/users/{alice.id}",
            "/settings",
            "/log",
            f"/searches/{search.id}",
            f"/trips/{trip.id}",
        ):
            response = client.get(url)
            assert response.status_code == 200, url
        assert "Sharing" in client.get(f"/trips/{trip.id}").text
        token = login_as(client, "alice")
        for url in ("/settings", "/log", "/searches/new", "/trips/new", f"/trips/{trip.id}"):
            assert client.get(url).status_code == 200, url
        response = client.post(
            f"/trips/{trip.id}/shares",
            data={"csrf_token": token, "username": "admin", "permission": "view"},
        )
        assert "is an admin and sees everything already" in response.text

    def test_trip_page_title_holds_only_the_trip_name(self, client, db):
        alice = add_user(db, "alice")
        trip = create_trip(db, trip_input(), max_route_pairs=50, owner_id=alice.id)
        db.commit()
        login_as(client, "alice")
        page = client.get(f"/trips/{trip.id}").text
        title = page.split("<title>", 1)[1].split("</title>", 1)[0]
        # The sharing form (with the CSRF token) once leaked into the browser tab title.
        assert " ".join(title.split()) == f"{trip.name} · Plane and simple"
        assert page.count("<summary>Sharing</summary>") == 1
