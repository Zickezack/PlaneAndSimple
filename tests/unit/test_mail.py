import smtplib

import pytest

from flighttracker.config import Settings
from flighttracker.domain.emails import is_valid_email
from flighttracker.security.secrets import decrypt, encrypt
from flighttracker.services import mail
from flighttracker.services.settings import SettingValidationError, validate
from flighttracker.web.settings_forms import mail_preset

KEY = "k" * 40


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://unused/unused_test",
        "secret_key": KEY,
        "admin_username": "admin",
        "admin_password_hash": "unused",
        "smtp_host": "smtp.example.test",
        "smtp_username": "me@example.test",
        "smtp_password": "app-password",
        "mail_from": "me@example.test",
    }
    return Settings(**(values | overrides))


class FakeSMTP:
    """Records what the app does with the connection instead of talking to a server."""

    calls: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        if password != "app-password":
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        FakeSMTP.calls.append(("login", user))

    def send_message(self, message):
        FakeSMTP.calls.append(("send", message["To"], message["Subject"]))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.calls = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.calls


def test_secrets_round_trip_and_wrong_key():
    stored = encrypt("app-password", KEY)
    assert stored.startswith("fernet:") and "app-password" not in stored
    assert decrypt(stored, KEY) == "app-password"
    assert decrypt(stored, "another-key-" * 4) is None
    assert decrypt("plain text", KEY) is None


def test_send_with_starttls_and_login(fake_smtp):
    mail.send(settings(), mail.Mail("you@example.test", "Hello", "Body"))
    assert fake_smtp == [
        ("connect", "smtp.example.test", 587),
        ("starttls",),
        ("login", "me@example.test"),
        ("send", "you@example.test", "Hello"),
    ]


def test_send_errors_are_safe_and_logged(fake_smtp):
    with pytest.raises(mail.MailError, match="rejected the login") as error:
        mail.send(settings(smtp_password="wrong"), mail.Mail("you@example.test", "Hi", "x"))
    assert "wrong" not in str(error.value)
    with pytest.raises(mail.MailError, match="not set up"):
        mail.send(settings(smtp_host=""), mail.Mail("you@example.test", "Hi", "x"))


def test_links_use_the_public_address():
    assert mail.link(settings(public_base_url="https://f.example.ch"), "/x") == (
        "https://f.example.ch/x"
    )
    assert mail.link(settings(), "/x", base_url="http://localhost:8000/") == (
        "http://localhost:8000/x"
    )
    assert mail.link(settings(), "/x") is None


def test_mail_settings_validation():
    assert validate("smtp_security", "ssl") == "ssl"
    assert (
        validate("public_base_url", "https://flights.example.ch/") == "https://flights.example.ch"
    )
    assert validate("mail_from", "app@example.ch") == "app@example.ch"
    for key, raw in (
        ("smtp_security", "tls"),
        ("public_base_url", "flights.example.ch"),
        ("mail_from", "app@"),
        ("smtp_port", "70000"),
    ):
        with pytest.raises(SettingValidationError):
            validate(key, raw)


def test_provider_presets():
    assert mail_preset("gmail") == {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "587",
        "smtp_security": "starttls",
    }
    assert mail_preset("") == {}


def test_email_addresses():
    assert is_valid_email("reto@example.ch")
    assert not is_valid_email("reto@example")
    assert not is_valid_email("re to@example.ch")


def test_every_mail_has_an_html_part_made_from_the_text(monkeypatch):
    sent = []

    class Capture(FakeSMTP):
        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(mail.smtplib, "SMTP", Capture)
    body = "Hello <you>\n\n• ZRH → BCN: 120.00 CHF\n• ZRH → MAD: 99.00 CHF\n\nAll prices:\nhttps://f.example/x?a=1&b=2"
    mail.send(settings(), mail.Mail("you@example.test", "Price alert", body))
    (message,) = sent
    assert message.get_content_type() == "multipart/alternative"
    text, html = (part.get_content() for part in message.iter_parts())
    assert text.startswith("Hello <you>")
    assert "Hello &lt;you&gt;" in html  # escaped
    assert "<li>ZRH → BCN: 120.00 CHF</li>" in html
    assert '<a href="https://f.example/x?a=1&amp;b=2"' in html
