"""AI in the web: draft from a wish, 'when to book' advice and its explanation, connection test."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from flighttracker.config import Settings
from flighttracker.security.passwords import hash_password
from flighttracker.services import ai
from flighttracker.web.app import create_app
from tests.integration.test_services import create
from tests.integration.test_web import PASSWORD, _seed_price, login


def ai_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://unused/unused_test",
        "secret_key": "x" * 40,
        "admin_username": "admin",
        "admin_password_hash": hash_password(PASSWORD, n=2**10),
        "session_cookie_secure": False,
        "ai_provider": "ollama",
        "ai_base_url": "http://ollama:11434/v1",
        "ai_model": "llama3.2",
    }
    return Settings(**(values | overrides))


@pytest.fixture
def client(db, session_factory):
    with TestClient(create_app(ai_settings(), session_factory)) as test_client:
        yield test_client


@pytest.fixture
def model(monkeypatch):
    """The AI's next answer; records the prompts it got."""
    prompts = []

    def answer(text):
        def post(url, json=None, headers=None, timeout=None):
            prompts.append(json["messages"][-1]["content"])
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        monkeypatch.setattr(ai.httpx, "post", post)
        return prompts

    return answer


def test_draft_fills_the_form_without_saving(client, db, model):
    draft = {
        "name": "Zurich to Barcelona",
        "origins": ["ZRH"],
        "destinations": ["Spain", "Atlantis"],
        "trip_type": "round_trip",
        "cabin_classes": ["economy"],
        "max_stops": "0",
        "months_ahead": 3,
        "stay_days_min": 5,
        "stay_days_max": 7,
        "adults": 2,
        "children": 0,
        "notes": "Exact dates are tracked as whole months.",
    }
    model(json.dumps(draft))
    token = login(client)
    page = client.get("/searches/new").text
    assert "Describe your trip" in page
    response = client.post(
        "/searches/draft", data={"csrf_token": token, "wish": "Two of us to Barcelona, direct"}
    )
    page = response.text
    assert 'name="name" type="text" value="Zurich to Barcelona"' in page
    assert 'name="destinations" type="text" value="ES"' in page  # "Spain" looked up
    assert "Atlantis" in page and "please add by hand" in page
    assert "Exact dates are tracked as whole months." in page
    assert "Two of us to Barcelona, direct" in page  # the wish stays in its field
    assert client.get("/searches").text.count("Zurich to Barcelona") == 0  # nothing saved


def test_advice_on_the_flight_page_and_its_explanation(client, db, model):
    search = create(db)
    db.commit()
    now = datetime.now(UTC)
    _seed_price(db, search.id, price="300.00", observed_at=now - timedelta(days=2))
    row = _seed_price(db, search.id, price="250.00", observed_at=now)
    token = login(client)
    detail = client.get(f"/searches/{search.id}").text
    assert "Good moment to book" in detail  # badge in the flights table
    page = client.get(f"/searches/{search.id}/flights/{row.id}").text
    assert "When to book" in page and "The lowest price seen so far." in page
    assert "Explain with AI" in page

    prompts = model("The price fell to its lowest so far; booking now looks sensible.")
    page = client.post(
        f"/searches/{search.id}/flights/{row.id}/explain", data={"csrf_token": token}
    ).text
    assert "booking now looks sensible" in page and "Written by AI" in page
    assert "300.00 CHF" in prompts[0] and "250.00 CHF" in prompts[0]  # the app's numbers


def test_connection_test_reports_errors(client, db, monkeypatch):
    def unreachable(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ai.httpx, "post", unreachable)
    token = login(client)
    response = client.post("/settings/ai/test", data={"csrf_token": token})
    assert "AI not reachable" in response.text and "No connection" in response.text
