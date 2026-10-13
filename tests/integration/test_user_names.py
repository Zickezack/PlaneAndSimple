"""Renaming users and logging in with the e-mail address."""

import pytest

from flighttracker.models import User
from flighttracker.services.login import authenticate
from flighttracker.services.users import UserValidationError, rename_user, set_email
from tests.integration.test_services import create
from tests.integration.test_users import USER_PASSWORD, _settings, add_user, login_as
from tests.integration.test_web import client, csrf, login  # noqa: F401 (fixture)


def test_login_with_username_or_e_mail_address(client, db):  # noqa: F811
    alice = add_user(db, "alice", email="Alice@Example.test")
    settings = _settings()
    for login_name in ("alice", "ALICE", "alice@example.test", " Alice@Example.TEST "):
        assert authenticate(db, settings, login_name, USER_PASSWORD) is alice
    assert authenticate(db, settings, "alice@example.test", "wrong-password-123") is None
    alice.is_active = False
    assert authenticate(db, settings, "alice@example.test", USER_PASSWORD) is None

    add_user(db, "bob", email="bob@example.test")
    login_as(client, "bob@example.test")  # through the login form
    assert "Logged in as bob" in client.get("/settings").text


def test_env_admin_logs_in_with_its_e_mail_address(client, db):  # noqa: F811
    login(client)  # creates the env admin's row
    admin = db.query(User).filter_by(username="admin").one()
    set_email(db, admin, "root@example.test")
    db.commit()
    client.cookies.clear()
    token = csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "root@example.test",
            "password": "a-very-long-test-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_usernames_and_addresses_never_collide(db):
    settings = _settings()
    add_user(db, "carol", email="carol@example.test")
    dave = add_user(db, "dave")
    # A username may contain "@" – but not be another user's address, and vice versa.
    with pytest.raises(UserValidationError):
        rename_user(db, dave, settings, "carol@example.test")
    with pytest.raises(UserValidationError):
        set_email(db, dave, "Carol@example.test")
    erin = add_user(db, "erin@example.test")
    with pytest.raises(UserValidationError):
        set_email(db, dave, "erin@example.test")
    assert erin.username == "erin@example.test"


def test_admin_renames_a_user(client, db):  # noqa: F811
    frank = add_user(db, "frank", email="frank@example.test")
    search = create(db)
    search.owner_id = frank.id
    db.commit()
    token = login(client)
    form = {"csrf_token": token, "role": "user", "is_active": "1", "email": "frank@example.test"}
    client.post(f"/users/{frank.id}", data=form | {"username": "Franziska"})
    db.refresh(frank)
    assert frank.username == "Franziska" and search.owner_id == frank.id
    client.post(f"/users/{frank.id}", data=form | {"username": "admin"})  # the .env admin's
    db.refresh(frank)
    assert frank.username == "Franziska"
    page = client.get(f"/users/{frank.id}").text
    assert 'name="username" type="text" value="Franziska"' in page

    login_as(client, "franziska")
    assert "Logged in as Franziska" in client.get("/settings").text


def test_env_admin_cannot_be_renamed(client, db):  # noqa: F811
    login(client)
    admin = db.query(User).filter_by(username="admin").one()
    with pytest.raises(UserValidationError):
        rename_user(db, admin, _settings(), "boss")
    page = client.get(f"/users/{admin.id}").text
    assert '<input type="hidden" name="username" value="admin">' in page
