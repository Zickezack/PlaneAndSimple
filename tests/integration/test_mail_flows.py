"""Mail: welcome link, forgot password, notices, e-mail addresses, price alerts, SMTP password."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flighttracker.config import Settings
from flighttracker.models import PasswordToken, PlatformSetting, PriceAlert, User
from flighttracker.security.passwords import hash_password
from flighttracker.services import mail, price_alerts
from flighttracker.services.settings import effective_settings
from flighttracker.web.app import create_app
from tests.integration.test_services import create
from tests.integration.test_web import PASSWORD, _seed_price, csrf, login

PUBLIC = "https://flights.example.test"


def mail_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://unused/unused_test",
        "secret_key": "x" * 40,
        "admin_username": "admin",
        "admin_password_hash": hash_password(PASSWORD, n=2**10),
        "session_cookie_secure": False,
        "smtp_host": "smtp.example.test",
        "mail_from": "app@example.test",
        "public_base_url": PUBLIC,
    }
    return Settings(**(values | overrides))


@pytest.fixture
def outbox(monkeypatch) -> list[mail.Mail]:
    sent: list[mail.Mail] = []
    monkeypatch.setattr(mail, "send", lambda settings, message: sent.append(message))
    return sent


@pytest.fixture
def client(db, session_factory):
    with TestClient(create_app(mail_settings(), session_factory)) as test_client:
        yield test_client


def link_in(message: mail.Mail) -> str:
    return re.search(rf"{PUBLIC}(/\S+)", message.body).group(1)


def test_welcome_link_sets_the_first_password(client, db, outbox):
    token = login(client)
    client.post(
        "/users",
        data={
            "csrf_token": token,
            "username": "alice",
            "email": "alice@example.test",
            "role": "user",
        },
    )
    (welcome,) = outbox
    assert welcome.to == "alice@example.test" and "alice" in welcome.body
    path = link_in(welcome)
    assert path.startswith("/password/reset/")

    client.post("/logout", data={"csrf_token": token})
    client.cookies.clear()
    page = client.get(path)
    assert page.status_code == 200
    response = client.post(
        path,
        data={
            "csrf_token": csrf(page.text),
            "new_password": "a-new-long-password",
            "repeat_password": "a-new-long-password",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login"
    assert "was changed" in outbox[-1].subject
    assert client.get(path).status_code == 404  # used once
    page = client.get("/login").text
    response = client.post(
        "/login",
        data={"csrf_token": csrf(page), "username": "alice", "password": "a-new-long-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_forgot_password_reveals_nothing(client, db, outbox):
    user = User(username="bob", password_hash=hash_password("x" * 12, n=2**10), email="b@e.test")
    db.add_all([user, User(username="carol", password_hash=hash_password("x" * 12, n=2**10))])
    db.commit()
    page = client.get("/login").text
    assert 'href="/password/forgot"' in page
    token = csrf(client.get("/password/forgot").text)
    answers = []
    for login_name in ("nobody", "carol", "admin", "B@E.TEST"):
        response = client.post("/password/forgot", data={"csrf_token": token, "login": login_name})
        answers.append(response.text.count("a link to set a new password is on its way"))
    assert answers == [1, 1, 1, 1]
    # Only bob has an address (the .env admin's password lives in .env).
    assert [m.to for m in outbox] == ["b@e.test"]
    assert "60 minutes" in outbox[0].body


def test_expired_link_is_refused(client, db, outbox):
    user = User(username="dave", password_hash=hash_password("x" * 12, n=2**10), email="d@e.test")
    db.add(user)
    db.commit()
    client.post(
        "/password/forgot",
        data={"csrf_token": csrf(client.get("/password/forgot").text), "login": "dave"},
    )
    path = link_in(outbox[0])
    row = db.scalars(select(PasswordToken).where(PasswordToken.user_id == user.id)).one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert client.get(path).status_code == 404


def test_e_mail_address_in_personal_settings(client, db, outbox):
    db.add(User(username="erin", password_hash=hash_password("x" * 12, n=2**10), email="e@e.test"))
    db.commit()
    token = login(client)
    personal = {"csrf_token": token, "locale": "en", "timezone": ""}
    client.post("/settings/personal", data=personal | {"email": "E@E.test"})
    admin = db.scalars(select(User).where(User.username == "admin")).one()
    db.refresh(admin)
    assert admin.email is None  # taken by erin (ignoring case)
    client.post("/settings/personal", data=personal | {"email": "admin@example.test"})
    db.refresh(admin)
    assert admin.email == "admin@example.test"
    client.post("/settings/mail/test", data={"csrf_token": token})
    assert [m.to for m in outbox] == ["admin@example.test"]


def test_price_alert_once_per_flight_and_drop(client, db, outbox):
    search = create(db)
    db.commit()
    token = login(client)
    alerts_url = f"/searches/{search.id}/alerts"
    client.post(alerts_url, data={"csrf_token": token, "max_price": "200"})
    assert db.scalars(select(PriceAlert)).all() == []  # no e-mail address yet
    client.post(
        "/settings/personal",
        data={"csrf_token": token, "locale": "en", "timezone": "", "email": "a@e.test"},
    )
    client.post(alerts_url, data={"csrf_token": token, "max_price": "200"})
    client.post(alerts_url, data={"csrf_token": token, "max_price": "500", "route": "GVA-MAD"})
    (alert,) = db.scalars(select(PriceAlert)).all()  # the second route is not part of it
    assert "Price up to (CHF)" in client.get(f"/searches/{search.id}").text

    settings = effective_settings(db, mail_settings())
    now = datetime.now(UTC)
    _seed_price(db, search.id, price="250.00")
    assert price_alerts.notify(db, settings, search, now) == 0  # above the alert price
    _seed_price(db, search.id, price="180.00", observed_at=now + timedelta(minutes=1))
    assert price_alerts.notify(db, settings, search, now) == 1
    assert "180.00 CHF" in outbox[-1].body and f"{PUBLIC}/searches/{search.id}" in outbox[-1].body
    assert price_alerts.notify(db, settings, search, now) == 0  # reported already
    _seed_price(db, search.id, price="185.00", observed_at=now + timedelta(minutes=2))
    assert price_alerts.notify(db, settings, search, now) == 0  # dearer than reported
    _seed_price(db, search.id, price="170.00", observed_at=now + timedelta(minutes=3))
    assert price_alerts.notify(db, settings, search, now) == 1  # a new drop
    assert len(outbox) == 2

    client.post(f"{alerts_url}/{alert.id}/delete", data={"csrf_token": token})
    assert db.scalars(select(PriceAlert)).all() == []


def test_smtp_password_is_stored_encrypted(client, db, monkeypatch):
    checked = []
    monkeypatch.setattr(mail, "check_login", lambda settings: checked.append(settings.smtp_host))
    token = login(client)
    response = client.post(
        "/settings/section/mail",
        data={
            "csrf_token": token,
            "smtp_preset": "gmail",
            "smtp_username": "me@gmail.com",
            "smtp_password": "app-password",
            "mail_from": "me@gmail.com",
            "smtp_port": "25",
        },
    )
    assert checked == ["smtp.gmail.com"]  # the login is checked right after saving
    # The preset filled the untouched host; the port typed by hand (25) wins over its 587.
    assert "Login to smtp.gmail.com:25 works." in response.text
    stored = {row.key: row.value for row in db.scalars(select(PlatformSetting))}
    assert stored["smtp_host"] == "smtp.gmail.com" and stored["smtp_port"] == "25"
    assert stored["smtp_password"].startswith("fernet:")
    assert "app-password" not in stored["smtp_password"]
    effective = effective_settings(db, mail_settings())
    assert effective.smtp_password.get_secret_value() == "app-password"
    # With another SECRET_KEY the stored password is unusable and ignored.
    other = effective_settings(db, mail_settings(secret_key="y" * 40))
    assert other.smtp_password is None


def test_worker_checks_alerts_after_a_completed_poll(db, session_factory, monkeypatch):
    from flighttracker.providers.mock import MockProvider
    from flighttracker.worker import loop

    search = create(db)
    db.commit()
    checked = []
    monkeypatch.setattr(
        loop.price_alerts, "notify", lambda session, settings, s, now: checked.append(s.id) or 0
    )
    now = datetime.now(UTC)
    loop.schedule(session_factory, clock=lambda: now)
    assert loop.process_next_job(
        session_factory,
        MockProvider(clock=lambda: now),
        clock=lambda: now,
        base_settings=mail_settings(),
    )
    assert checked == [search.id]


def test_mail_log_and_welcome_again(client, db, outbox, monkeypatch):
    from flighttracker.models import MailLog

    token = login(client)
    client.post(
        "/users",
        data={"csrf_token": token, "username": "gina", "email": "g@e.test", "role": "user"},
    )
    gina = db.scalars(select(User).where(User.username == "gina")).one()
    assert "No password yet" in client.get("/users").text
    page = client.get(f"/users/{gina.id}").text
    assert "Send the welcome mail again" in page

    def failing(settings, message):
        raise mail.MailError("No connection to smtp.example.test:587 (ConnectionRefusedError).")

    monkeypatch.setattr(mail, "send", failing)
    response = client.post(f"/users/{gina.id}/welcome", data={"csrf_token": token})
    assert "could not be sent" in response.text
    monkeypatch.setattr(mail, "send", lambda settings, message: outbox.append(message))
    client.post(f"/users/{gina.id}/welcome", data={"csrf_token": token})
    first, second = (link_in(m) for m in outbox)
    assert first != second and client.get(first).status_code == 404  # the old link is void

    log = db.scalars(select(MailLog).order_by(MailLog.id)).all()
    assert [(e.kind, e.ok, e.user_id) for e in log] == [
        ("welcome", True, gina.id),
        ("welcome", False, gina.id),
        ("welcome", True, gina.id),
    ]
    assert "No connection" in log[1].error
    settings_page = client.get("/settings").text
    assert "Mail log" in settings_page and "2 sent, 1 failed" in settings_page
