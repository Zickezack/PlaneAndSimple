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
        ai_provider="openai",
        ai_base_url="http://lmstudio:1234/v1/",
        ai_model="qwen",
        ai_api_key=None,
    )
    assert ai.complete_json(local, "s", "u", {"type": "object"}) == {"a": 1}
    call = fake.calls[0]
    assert call["url"] == "http://lmstudio:1234/v1/chat/completions"
    assert call["headers"] == {}
    assert call["json"]["messages"][0] == {"role": "system", "content": "s"}
    assert call["json"]["response_format"] == {"type": "json_object"}


def ollama(**overrides) -> Settings:
    values = {
        "ai_provider": "ollama",
        "ai_base_url": "http://ollama:11434/v1",  # an OpenAI-style URL is accepted too
        "ai_model": "llama3.1:8b",
        "ai_api_key": None,
        "ai_ollama_num_ctx": 8192,
        "ai_ollama_keep_alive": "-1",
    }
    return settings(**(values | overrides))


def test_ollama_keeps_the_shared_model_loaded(post):
    fake = post(200, {"message": {"content": '{"a": 1}'}, "prompt_eval_count": 30, "eval_count": 5})
    answer = ai.complete(ollama(), "s", "u", schema={"type": "object"})
    assert (answer.text, answer.input_tokens, answer.output_tokens) == ('{"a": 1}', 30, 5)
    call = fake.calls[0]
    assert call["url"] == "http://ollama:11434/api/chat"
    body = call["json"]
    assert body["options"] == {"num_ctx": 8192, "num_predict": 8000}
    assert body["keep_alive"] == -1 and body["stream"] is False
    assert body["format"] == {"type": "object"}


def test_ollama_without_fixed_values_and_a_tight_context(post):
    fake = post(200, {"message": {"content": "ok"}})
    ai.complete(ollama(ai_ollama_num_ctx=0, ai_ollama_keep_alive=""), "s", "u")
    body = fake.calls[0]["json"]
    assert "keep_alive" not in body and body["options"] == {"num_predict": 8000}
    ai.complete(ollama(ai_ollama_num_ctx=1000, ai_ollama_keep_alive="30m"), "s" * 2000, "u")
    body = fake.calls[1]["json"]
    assert body["keep_alive"] == "30m"
    assert body["options"]["num_predict"] < 1000  # the answer must fit next to the prompt
    with pytest.raises(ai.AIError, match="does not fit"):
        ai.complete(ollama(ai_ollama_num_ctx=600), "s" * 2000, "u")


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


def test_thinking_is_never_part_of_the_answer(post):
    assert ai.strip_thinking('<think>hmm, JSON?</think>{"a": 1}') == '{"a": 1}'
    assert ai.strip_thinking("Fine.\n<Thinking>x</Thinking> Book now.") == "Fine.\n Book now."
    assert ai.strip_thinking("Answer <think>cut off by the limit") == "Answer "
    fake = post(200, {"message": {"content": '<think>let me see</think>{"a": 2}'}})
    assert ai.complete_json(ollama(ai_ollama_no_thinking=True), "s", "u", {}) == {"a": 2}
    assert fake.calls[0]["json"]["think"] is False
    ai.complete(ollama(), "s", "u")
    assert "think" not in fake.calls[1]["json"]  # only when switched on


class FakeSession:
    """Collects the usage log rows (`ai_log.record` only adds, prunes and flushes)."""

    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    def execute(self, statement):
        return None

    def flush(self):
        return None


def test_the_log_keeps_every_stage_of_a_cut_off_answer(post):
    post(
        200,
        {
            "message": {"role": "assistant", "content": '{"name": "Lis', "thinking": "Lisbon."},
            "done_reason": "length",
            "prompt_eval_count": 447,
            "eval_count": 512,
            "load_duration": 12_000_000,
        },
    )
    session = FakeSession()
    usage = ai.UsageLog(session, "draft", 7)
    with pytest.raises(ai.AIError, match="response token limit"):
        ai.complete_json(ollama(ai_max_output_tokens=512), "system", "wish", {}, usage=usage)
    row = session.added[0]
    assert not row.ok and row.cut_off and row.stop_reason == "length"
    assert (row.input_tokens, row.output_tokens, row.output_limit) == (447, 512, 512)
    assert row.request["url"] == "http://ollama:11434/api/chat"
    assert row.request["body"]["options"] == {"num_predict": 512, "num_ctx": 8192}
    assert "messages" not in row.request["body"]  # the prompts have their own columns
    assert (row.system_prompt, row.user_prompt) == ("system", "wish")
    assert (row.thinking, row.answer, row.result) == ("Lisbon.", '{"name": "Lis', None)
    assert row.response["load_duration"] == 12_000_000
    assert row.response["message"] == {"role": "assistant"}  # the texts are not repeated


def test_thinking_of_every_provider_and_never_the_key(post):
    session = FakeSession()
    usage = ai.UsageLog(session, "explain")
    blocks = [{"type": "thinking", "thinking": "Prices fall."}, {"type": "text", "text": "Wait."}]
    post(200, {"content": blocks, "stop_reason": "end_turn"})
    assert ai.complete(settings(), "s", "u", usage=usage).text == "Wait."
    message = {"content": "Book. sk-secret", "reasoning_content": "Lowest price."}
    post(200, {"choices": [{"message": message, "finish_reason": "stop"}]})
    remote = settings(ai_provider="openai", ai_base_url="https://me:pw@api.example/v1")
    ai.complete(remote, "s", "u", usage=usage)
    post(200, {"message": {"content": "<think>Hmm.</think>Watch."}})
    assert ai.complete(ollama(), "s", "u", usage=usage).text == "Watch."
    assert [row.thinking for row in session.added] == ["Prices fall.", "Lowest price.", "Hmm."]
    assert session.added[0].response["content"] == ["thinking", "text"]
    assert session.added[1].request["url"] == "https://api.example/v1/chat/completions"
    assert session.added[1].answer == "Book. ***"  # a key never ends up in the log
    assert all("sk-secret" not in repr(vars(row)) for row in session.added)

    ai.complete(ollama(ai_log_messages=False), "s", "u", usage=usage)
    row = session.added[-1]
    assert row.ok and row.request and (row.system_prompt, row.answer, row.result) == (None,) * 3


def test_failed_answers_are_logged_with_what_came_back(post):
    session = FakeSession()
    usage = ai.UsageLog(session, "draft")
    post(200, answer_anthropic("Sure! Here is your draft."))
    with pytest.raises(ai.AIError, match="did not answer with JSON"):
        ai.complete_json(settings(), "s", "u", {"type": "object"}, usage=usage)
    post(404, {"error": "model 'qwen9' not found"})
    with pytest.raises(ai.AIError, match="not found"):
        ai.complete(ollama(), "s", "u", usage=usage)
    post(401, {"error": "Incorrect API key provided: sk-sec***"})
    with pytest.raises(ai.AIError, match="rejected"):
        ai.complete(settings(), "s", "u", usage=usage)
    no_json, missing, rejected = session.added
    assert not no_json.ok and not no_json.cut_off and no_json.answer == "Sure! Here is your draft."
    assert "qwen9" in missing.answer and missing.request["body"]["model"] == "llama3.1:8b"
    assert rejected.answer is None  # such answers can repeat part of the key


def test_split_thinking_returns_both_parts():
    assert ai.split_thinking("<think>a</think>Yes<think>b</think>") == ("Yes", "a\n\nb")
    assert ai.split_thinking("No <think>cut off") == ("No ", "cut off")
