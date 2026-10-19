"""Platform Settings sections: presets never overwrite edits, a wrong field never costs the
rest of the input, tests use the values as entered, and the script gets JSON (no reload)."""

import httpx
import pytest
from sqlalchemy import select

from flighttracker.models import PlatformSetting
from flighttracker.services import ai, mail
from tests.integration.test_web import client, login  # noqa: F401 (fixture)

JSON = {"Accept": "application/json"}
AI_FORM = {
    "ai_provider": "ollama",
    "ai_base_url": "http://gpu-box:11434",
    "ai_model": "qwen3:8b",
    "ai_ollama_num_ctx": "8192",
    "ai_ollama_keep_alive": "-1",
    "ai_ollama_no_thinking": "true",
    "ai_max_input_tokens": "16000",
    "ai_max_output_tokens": "4000",
    "ai_drafts": "true",
    "ai_explanations": "true",
}


def stored(db) -> dict[str, str]:
    db.expire_all()
    return {row.key: row.value for row in db.scalars(select(PlatformSetting))}


@pytest.fixture
def token(client):  # noqa: F811
    return login(client)


def test_a_preset_never_overwrites_what_was_typed(client, db, token):  # noqa: F811
    # The Ollama preset was chosen (the script filled the fields), then URL and model edited.
    data = {"csrf_token": token, "ai_preset": "ollama", **AI_FORM}
    response = client.post("/settings/section/ai", data=data, headers=JSON)
    assert response.status_code == 200 and response.json()["ok"]
    values = stored(db)
    assert values["ai_base_url"] == "http://gpu-box:11434" and values["ai_model"] == "qwen3:8b"
    assert values["ai_ollama_num_ctx"] == "8192" and values["ai_ollama_no_thinking"] == "true"

    # Without JavaScript: the preset fills the fields still showing the stored value – only
    # the model was changed by hand, and that edit wins.
    untouched = {**AI_FORM, "ai_preset": "anthropic", "ai_model": "claude-haiku-5-5"}
    client.post("/settings/section/ai", data={"csrf_token": token, **untouched})
    values = stored(db)
    assert values["ai_provider"] == "anthropic"
    assert values["ai_base_url"] == "https://api.anthropic.com"
    assert values["ai_model"] == "claude-haiku-5-5"


def test_one_wrong_field_marks_it_and_keeps_the_rest(client, db, token):  # noqa: F811
    client.post("/settings/section/ai", data={"csrf_token": token, **AI_FORM}, headers=JSON)
    wrong = {**AI_FORM, "ai_ollama_keep_alive": "forever", "ai_model": "llama3.1"}
    response = client.post(
        "/settings/section/ai", data={"csrf_token": token, **wrong}, headers=JSON
    )
    data = response.json()
    assert response.status_code == 400 and not data["ok"]
    assert list(data["errors"]) == ["ai_ollama_keep_alive"]
    assert data["errors"]["ai_ollama_keep_alive"].startswith("Ollama: keep model loaded:")
    assert "correct the marked fields" in data["messages"][0]["text"]
    assert stored(db)["ai_model"] == "qwen3:8b"  # nothing stored while a field is wrong

    # Without JavaScript the page comes back with every value as typed.
    page = client.post("/settings/section/ai", data={"csrf_token": token, **wrong}).text
    assert 'name="ai_model" type="text" value="llama3.1"' in page
    assert 'name="ai_ollama_keep_alive" type="text" value="forever"' in page
    assert '<details class="card more" id="ai" open>' in page


def test_the_ai_test_uses_the_entered_values_without_saving(client, db, token, monkeypatch):  # noqa: F811
    calls = []

    def answer(url, json=None, headers=None, timeout=None):
        calls.append((url, json["model"], json["options"]["num_ctx"], json.get("think")))
        return httpx.Response(200, json={"message": {"content": "OK"}})

    monkeypatch.setattr(ai.httpx, "post", answer)
    monkeypatch.setattr(ai, "ollama_loaded", lambda settings: [])
    response = client.post("/settings/ai/test", data={"csrf_token": token, **AI_FORM}, headers=JSON)
    data = response.json()
    assert data["ok"] and data["messages"][0]["category"] == "success"
    assert calls == [("http://gpu-box:11434/api/chat", "qwen3:8b", 8192, False)]
    assert stored(db) == {}  # tested, not saved


def test_mail_checks_use_the_entered_values(client, db, token, monkeypatch):  # noqa: F811
    checked = []
    monkeypatch.setattr(mail, "check_login", lambda settings: checked.append(settings.smtp_host))
    form = {
        "csrf_token": token,
        "smtp_preset": "infomaniak",  # fills host, port and encryption (all untouched)
        "smtp_username": "me@example.ch",
        "smtp_password": "secret-app-password",
        "mail_from": "me@example.ch",
    }
    data = client.post("/settings/mail/check", data=form, headers=JSON).json()
    assert checked == ["mail.infomaniak.com"]
    assert data["messages"][0]["text"] == "Login to mail.infomaniak.com:587 works."
    assert stored(db) == {}

    data = client.post("/settings/section/mail", data=form, headers=JSON).json()
    assert data["ok"] and data["secrets"] == {"smtp_password": True}
    assert [m["category"] for m in data["messages"]] == ["success", "success"]  # saved + login
    assert "Mail server" not in data["setup_html"].split("Set up:")[0]  # no longer missing
    assert stored(db)["smtp_password"].startswith("fernet:")


def test_field_errors_in_the_users_language(client, db, token):  # noqa: F811
    client.get("/language/de")
    wrong = {**AI_FORM, "ai_ollama_keep_alive": "forever"}
    data = client.post(
        "/settings/section/ai", data={"csrf_token": token, **wrong}, headers=JSON
    ).json()
    assert data["errors"]["ai_ollama_keep_alive"] == (
        "Ollama: Modell geladen halten: Verwende -1 (für immer), 0 oder eine Dauer wie 30m oder 24h."
    )
    assert data["messages"][0]["text"].startswith("Nichts gespeichert oder getestet")
