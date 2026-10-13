import json
from datetime import date

import httpx
import pytest

from flighttracker.config import Settings
from flighttracker.services import ai, ai_prompts
from flighttracker.web.forms import default_values


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://unused/unused_test",
        "secret_key": "k" * 40,
        "admin_username": "admin",
        "admin_password_hash": "unused",
        "ai_provider": "anthropic",
        "ai_base_url": "https://api.anthropic.com",
        "ai_model": "claude-opus-5-5",
        "ai_api_key": "sk-secret",
    }
    return Settings(**(values | overrides))


class FakePost:
    """Records the request and answers like the provider would."""

    def __init__(self, status: int, payload):
        self.status, self.payload, self.calls = status, payload, []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return httpx.Response(self.status, json=self.payload)


def answer_anthropic(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop}


def answer_openai(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


@pytest.fixture
def post(monkeypatch):
    def install(status, payload):
        fake = FakePost(status, payload)
        monkeypatch.setattr(ai.httpx, "post", fake)
        return fake

    return install


def test_anthropic_request_and_json_output(post):
    fake = post(200, answer_anthropic('{"ok": true}'))
    data = ai.complete_json(settings(), "system", "user", {"type": "object"})
    assert data == {"ok": True}
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"] == {"x-api-key": "sk-secret", "anthropic-version": "2023-06-01"}
    assert call["json"]["system"] == "system" and call["json"]["model"] == "claude-opus-5-5"
    assert call["json"]["output_config"]["format"]["type"] == "json_schema"


def test_openai_compatible_request_without_key(post):
    fake = post(200, answer_openai('```json\n{"a": 1}\n```'))
    local = settings(
        ai_provider="ollama",
        ai_base_url="http://ollama:11434/v1/",
        ai_model="llama3.2",
        ai_api_key=None,
    )
    assert ai.complete_json(local, "s", "u", {"type": "object"}) == {"a": 1}
    call = fake.calls[0]
    assert call["url"] == "http://ollama:11434/v1/chat/completions"
    assert call["headers"] == {}
    assert call["json"]["messages"][0] == {"role": "system", "content": "s"}
    assert call["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (401, {"error": "bad key"}, "rejected the API key"),
        (429, {}, "rate or spending limit"),
        (200, answer_anthropic("", "refusal"), "declined"),
        (200, answer_anthropic("partial", "max_tokens"), "response token limit"),
        (200, answer_anthropic("   "), "empty answer"),
    ],
)
def test_errors_are_readable_and_never_show_the_key(post, status, payload, message):
    post(status, payload)
    with pytest.raises(ai.AIError, match=message) as error:
        ai.complete(settings(), "s", "u")
    assert "sk-secret" not in str(error.value)


def test_limits_and_switches():
    with pytest.raises(ai.AIError, match="not set up"):
        ai.complete(settings(ai_provider=""), "s", "u")
    with pytest.raises(ai.AIError, match="longer than the token limit"):
        ai.complete(settings(ai_max_input_tokens=1000), "s", "x" * 5000)


def test_system_prompt_always_carries_the_guardrails():
    custom = ai_prompts.system_prompt(settings(ai_system_prompt="Be a pirate."), "de")
    assert custom.startswith("Be a pirate.")
    assert "Never invent prices" in custom and custom.endswith("Answer in German.")
    assert ai_prompts.DEFAULT_SYSTEM_PROMPT in ai_prompts.system_prompt(settings(), "en")


def test_draft_becomes_form_values(post):
    draft = {
        "name": "Zurich → Lisbon, October",
        "origins": ["zrh", "Basel"],
        "destinations": ["LIS"],
        "trip_type": "round_trip",
        "cabin_classes": ["economy", "spaceship"],
        "max_stops": "0",
        "months_ahead": 40,
        "stay_days_min": 6,
        "stay_days_max": 8,
        "adults": 2,
        "children": 0,
        "notes": "October as a whole month.",
    }
    post(200, answer_anthropic(json.dumps(draft)))
    result = ai_prompts.draft_search(settings(), "Two of us to Lisbon", date(2026, 10, 14), "en")
    values, unresolved = ai_prompts.draft_values(result, default_values(settings()))
    assert values["origins"] == "ZRH" and values["destinations"] == "LIS"
    assert unresolved == {"origins": ["Basel"], "destinations": []}
    assert values["cabin_classes"] == ["economy"]  # unknown classes dropped
    assert (values["max_stops"], values["months_ahead"]) == ("0", "12")  # clamped to 12
    assert (values["stay_days_min"], values["stay_days_max"], values["adults"]) == ("6", "8", "2")
    one_way = ai_prompts.draft_values(draft | {"trip_type": "one_way"}, default_values(settings()))
    assert one_way[0]["stay_days_min"] == ""
