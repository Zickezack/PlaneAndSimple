"""Calls to the configured AI provider (Platform Settings → AI), synchronous, one answer each.

Three wire formats over plain HTTP (httpx, no provider SDK): Anthropic's Messages API, Ollama's
native chat API (it alone takes the context window and keep-alive – sent on every request so a
model shared with other platforms is never reloaded or unloaded early) and the OpenAI-compatible
chat completions API (OpenAI, LM Studio, Mistral, Groq, OpenRouter …).
The AI only ever words or drafts: every number it is given comes from the app, and every draft
goes through the normal validation before anything is saved (services/ai_prompts.py).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.ai_providers import ANTHROPIC, OLLAMA
from flighttracker.models import AIUsage

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90  # local models can be slow; the web request waits for the answer
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_URL = "https://api.anthropic.com"
# Rough size of a token, to keep requests below the configured limit without a tokenizer.
CHARS_PER_TOKEN = 3.5
# The usage log keeps half a year.
LOG_RETENTION = timedelta(days=183)


class AIError(Exception):
    """The provider could not answer; the message is safe to show (never contains the key)."""


@dataclass(frozen=True)
class AIAnswer:
    text: str
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class UsageLog:
    """Where `complete` logs the request (`ai_usage`; the caller commits)."""

    session: Session
    feature: str  # draft, explain, test
    user_id: int | None = None


Reply = tuple[str, int | None, int | None]  # text, input tokens, output tokens


def ai_enabled(settings: Settings) -> bool:
    return bool(settings.ai_provider and settings.ai_model and settings.ai_base_url)


def _key(settings: Settings) -> str:
    return settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""


def _anthropic(settings: Settings, system: str, user: str, schema: dict | None) -> Reply:
    body: dict = {
        "model": settings.ai_model,
        "max_tokens": settings.ai_max_output_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    base = (settings.ai_base_url or ANTHROPIC_URL).rstrip("/")
    response = httpx.post(
        f"{base}/v1/messages",
        json=body,
        headers={"x-api-key": _key(settings), "anthropic-version": ANTHROPIC_VERSION},
        timeout=TIMEOUT_SECONDS,
    )
    data = _json(response)
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AIError("The model declined to answer.")
    if stop == "max_tokens":
        raise AIError("The answer hit the response token limit – raise it under Platform Settings.")
    usage = data.get("usage") or {}
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, usage.get("input_tokens"), usage.get("output_tokens")


def _openai(settings: Settings, system: str, user: str, schema: dict | None) -> Reply:
    body: dict = {
        "model": settings.ai_model,
        "max_tokens": settings.ai_max_output_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if schema is not None:
        # The JSON mode every compatible server knows; the schema itself is in the prompt.
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {_key(settings)}"} if _key(settings) else {}
    response = httpx.post(
        f"{settings.ai_base_url.rstrip('/')}/chat/completions",
        json=body,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    data = _json(response)
    try:
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise AIError(
                "The answer hit the response token limit – raise it under Platform Settings."
            )
        usage = data.get("usage") or {}
        text = choice["message"]["content"] or ""
        return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
    except (KeyError, IndexError, TypeError):
        raise AIError("The provider's answer has an unexpected format.") from None


_THINKING = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def strip_thinking(text: str) -> str:
    """Without the reasoning some models write into their answer (<think>…</think>), so it
    never ends up in an explanation or breaks a JSON draft. An unclosed block (cut off by the
    token limit) leaves nothing usable."""
    text = _THINKING.sub("", text)
    opened = re.search(r"<(think|thinking|reasoning)>", text, re.I)
    return text[: opened.start()] if opened else text


def ollama_base(settings: Settings) -> str:
    """Ollama's own API lives at the server root (an OpenAI-style …/v1 URL is accepted too)."""
    return settings.ai_base_url.rstrip("/").removesuffix("/v1")


def _ollama(settings: Settings, system: str, user: str, schema: dict | None) -> Reply:
    options: dict = {"num_predict": settings.ai_max_output_tokens}
    if settings.ai_ollama_num_ctx:
        # Same context window as the loaded model, otherwise Ollama reloads it for us.
        options["num_ctx"] = settings.ai_ollama_num_ctx
        room = settings.ai_ollama_num_ctx - int((len(system) + len(user)) / CHARS_PER_TOKEN)
        if room < 256:
            raise AIError("The request does not fit into the Ollama context window.")
        options["num_predict"] = min(settings.ai_max_output_tokens, room)
    body: dict = {
        "model": settings.ai_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "options": options,
    }
    if settings.ai_ollama_keep_alive:
        # Without it every request resets the model's lifetime to Ollama's default (5 min).
        keep = settings.ai_ollama_keep_alive
        body["keep_alive"] = int(keep) if keep.lstrip("-").isdigit() else keep
    if settings.ai_ollama_no_thinking:
        # Thinking models would spend the answer's tokens on it (and older Ollama versions
        # put it into the text as <think>…</think>).
        body["think"] = False
    if schema is not None:
        body["format"] = schema
    response = httpx.post(f"{ollama_base(settings)}/api/chat", json=body, timeout=TIMEOUT_SECONDS)
    data = _json(response)
    if data.get("done_reason") == "length":
        raise AIError("The answer hit the response token limit – raise it under Platform Settings.")
    text = (data.get("message") or {}).get("content") or ""
    return text, data.get("prompt_eval_count"), data.get("eval_count")


@dataclass(frozen=True)
class LoadedModel:
    name: str
    context_length: int | None
    expires_at: str | None


def ollama_loaded(settings: Settings) -> list[LoadedModel]:
    """Models Ollama has in memory right now (`/api/ps`), for the connection test."""
    try:
        response = httpx.get(f"{ollama_base(settings)}/api/ps", timeout=10)
        models = _json(response).get("models") or []
    except (httpx.HTTPError, AIError):
        return []
    return [
        LoadedModel(
            m.get("name") or m.get("model") or "?", m.get("context_length"), m.get("expires_at")
        )
        for m in models
    ]


def _json(response: httpx.Response) -> dict:
    if response.status_code in (401, 403):
        raise AIError(f"The provider rejected the API key (HTTP {response.status_code}).")
    if response.status_code == 404:
        raise AIError("Model or API URL not found (HTTP 404) – check both.")
    if response.status_code == 429:
        raise AIError("The provider's rate or spending limit was reached (HTTP 429).")
    if response.status_code >= 400:
        raise AIError(f"The provider answered with HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise AIError("The provider's answer is not JSON.") from None


def complete(
    settings: Settings,
    system: str,
    user: str,
    *,
    schema: dict | None = None,
    usage: UsageLog | None = None,
) -> AIAnswer:
    """The model's answer to one request. `schema`: ask for JSON of this shape (the answer is
    still checked by the caller). `usage`: log the request, failed ones too."""
    started = time.monotonic()
    try:
        answer = _ask(settings, system, user, schema, started)
    except AIError as exc:
        _log(settings, usage, started, error=str(exc))
        raise
    _log(settings, usage, started, answer=answer)
    return answer


def _ask(settings: Settings, system: str, user: str, schema: dict | None, started) -> AIAnswer:
    if not ai_enabled(settings):
        raise AIError("AI is not set up (Platform Settings → AI).")
    if (len(system) + len(user)) / CHARS_PER_TOKEN > settings.ai_max_input_tokens:
        raise AIError("The request is longer than the token limit set under Platform Settings.")
    call = {ANTHROPIC: _anthropic, OLLAMA: _ollama}.get(settings.ai_provider, _openai)
    try:
        text, input_tokens, output_tokens = call(settings, system, user, schema)
    except httpx.TimeoutException:
        raise AIError(f"No answer within {TIMEOUT_SECONDS} seconds.") from None
    except httpx.HTTPError as exc:
        raise AIError(f"No connection to the provider ({type(exc).__name__}).") from None
    seconds = time.monotonic() - started
    log.info("AI answer from %s/%s in %.1f s", settings.ai_provider, settings.ai_model, seconds)
    text = strip_thinking(text)
    if not text.strip():
        raise AIError("The model returned an empty answer.")
    return AIAnswer(text.strip(), seconds, input_tokens, output_tokens)


def _log(
    settings: Settings,
    usage: UsageLog | None,
    started: float,
    *,
    answer: AIAnswer | None = None,
    error: str | None = None,
) -> None:
    if usage is None:
        return
    usage.session.add(
        AIUsage(
            user_id=usage.user_id,
            feature=usage.feature,
            provider=settings.ai_provider or "-",
            model=(settings.ai_model or "-")[:120],
            input_tokens=answer.input_tokens if answer else None,
            output_tokens=answer.output_tokens if answer else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=error is None,
            error=error,
        )
    )
    cutoff = datetime.now(UTC) - LOG_RETENTION
    usage.session.execute(delete(AIUsage).where(AIUsage.requested_at < cutoff))
    usage.session.flush()


def complete_json(
    settings: Settings, system: str, user: str, schema: dict, *, usage: UsageLog | None = None
) -> dict:
    """Like `complete`, parsed as a JSON object (code fences around it are tolerated)."""
    text = complete(settings, system, user, schema=schema, usage=usage).text
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AIError("The model did not answer with JSON.") from None
    if not isinstance(data, dict):
        raise AIError("The model did not answer with a JSON object.")
    return data
