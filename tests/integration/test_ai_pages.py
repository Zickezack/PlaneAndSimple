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
        "flight_provider": "mock",  # not from the container's environment
        "wikidata_contact": "",
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
            return httpx.Response(200, json={"message": {"content": text}})

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


def test_usage_log_records_requests_without_their_content(client, db, monkeypatch):
    from sqlalchemy import select

    from flighttracker.models import AIUsage

    def answer(url, json=None, headers=None, timeout=None):
        return httpx.Response(
            200, json={"message": {"content": "OK"}, "prompt_eval_count": 12, "eval_count": 1}
        )

    monkeypatch.setattr(ai.httpx, "post", answer)
    monkeypatch.setattr(ai, "ollama_loaded", lambda settings: [])
    token = login(client)
    client.post("/settings/ai/test", data={"csrf_token": token})
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: httpx.Response(429, json={}))
    client.post("/searches/draft", data={"csrf_token": token, "wish": "Somewhere warm"})
    rows = db.scalars(select(AIUsage).order_by(AIUsage.id)).all()
    assert [(r.feature, r.ok, r.input_tokens, r.output_tokens) for r in rows] == [
        ("test", True, 12, 1),
        ("draft", False, None, None),
    ]
    assert "429" in rows[1].error and rows[0].model == "llama3.2"
    page = client.get("/settings").text
    assert "Usage log" in page and "2 requests, 1 failed" in page


def test_connection_test_reports_how_ollama_holds_the_model(client, db, monkeypatch):
    monkeypatch.setattr(
        ai.httpx, "post", lambda *a, **k: httpx.Response(200, json={"message": {"content": "OK"}})
    )
    monkeypatch.setattr(
        ai,
        "ollama_loaded",
        lambda settings: [ai.LoadedModel("llama3.2:latest", 8192, "2318-03-01T10:00:00Z")],
    )
    token = login(client)
    page = client.post("/settings/ai/test", data={"csrf_token": token}).text
    assert "context window of 8&#39;192 tokens until it is unloaded by hand" in page  # ' escaped
    assert "No keep-alive set" in page and "No context window set" in page

    from flighttracker.services.settings import set_override

    set_override(db, "ai_ollama_num_ctx", "4096")
    set_override(db, "ai_ollama_keep_alive", "-1")
    db.commit()
    page = client.post("/settings/ai/test", data={"csrf_token": token}).text
    assert "differs from the loaded model" in page and "No keep-alive set" not in page


def test_settings_show_what_is_not_set_up(client, db):
    login(client)
    page = client.get("/settings").text
    setup = page.split('id="setup"')[1].split("</section>")[0]
    # AI is set up in this client; mail, real flight data and Wikidata are not.
    assert "Not set up yet" in setup and 'href="#mail"><strong>Mail server' in setup
    assert 'href="#providers"><strong>Flight data' in setup
    assert "Public address" not in setup  # only needed once mail is on
    assert "Set up:" in setup and ">AI</span>" in setup
