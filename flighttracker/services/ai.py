"""Calls to the configured AI provider (Platform Settings → AI), synchronous, one answer each.

Two wire formats over plain HTTP (httpx, no provider SDK): Anthropic's Messages API and the
OpenAI-compatible chat completions API (OpenAI, Ollama, LM Studio, Mistral, Groq, OpenRouter …).
The AI only ever words or drafts: every number it is given comes from the app, and every draft
goes through the normal validation before anything is saved (services/ai_prompts.py).
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

from flighttracker.config import Settings
from flighttracker.domain.ai_providers import ANTHROPIC

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90  # local models can be slow; the web request waits for the answer
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_URL = "https://api.anthropic.com"
# Rough size of a token, to keep requests below the configured limit without a tokenizer.
CHARS_PER_TOKEN = 3.5


class AIError(Exception):
    """The provider could not answer; the message is safe to show (never contains the key)."""


@dataclass(frozen=True)
class AIAnswer:
    text: str
    seconds: float


def ai_enabled(settings: Settings) -> bool:
    return bool(settings.ai_provider and settings.ai_model and settings.ai_base_url)


def _key(settings: Settings) -> str:
    return settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""


def _anthropic(settings: Settings, system: str, user: str, schema: dict | None) -> str:
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
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _openai(settings: Settings, system: str, user: str, schema: dict | None) -> str:
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
        return choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError("The provider's answer has an unexpected format.") from None


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


def complete(settings: Settings, system: str, user: str, *, schema: dict | None = None) -> AIAnswer:
    """The model's answer to one request. `schema`: ask for JSON of this shape (the answer is
    still checked by the caller)."""
    if not ai_enabled(settings):
        raise AIError("AI is not set up (Platform Settings → AI).")
    if (len(system) + len(user)) / CHARS_PER_TOKEN > settings.ai_max_input_tokens:
        raise AIError("The request is longer than the token limit set under Platform Settings.")
    call = _anthropic if settings.ai_provider == ANTHROPIC else _openai
    started = time.monotonic()
    try:
        text = call(settings, system, user, schema)
    except httpx.TimeoutException:
        raise AIError(f"No answer within {TIMEOUT_SECONDS} seconds.") from None
    except httpx.HTTPError as exc:
        raise AIError(f"No connection to the provider ({type(exc).__name__}).") from None
    seconds = time.monotonic() - started
    log.info("AI answer from %s/%s in %.1f s", settings.ai_provider, settings.ai_model, seconds)
    if not text.strip():
        raise AIError("The model returned an empty answer.")
    return AIAnswer(text.strip(), seconds)


def complete_json(settings: Settings, system: str, user: str, schema: dict) -> dict:
    """Like `complete`, parsed as a JSON object (code fences around it are tolerated)."""
    text = complete(settings, system, user, schema=schema).text
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AIError("The model did not answer with JSON.") from None
    if not isinstance(data, dict):
        raise AIError("The model did not answer with a JSON object.")
    return data
