"""Requests to the configured AI provider (Platform Settings → AI), synchronous, one answer each.

`complete` checks the request, sends it in the provider's wire format (services/ai_wire.py),
takes reasoning a model writes into its answer out of it and logs the whole exchange
(services/ai_log.py) – failed and cut-off answers too.
The AI only ever words or drafts: every number it is given comes from the app, and every draft
goes through the normal validation before anything is saved (services/ai_prompts.py).
"""

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from flighttracker.config import Settings
from flighttracker.services.ai_log import UsageLog, record
from flighttracker.services.ai_wire import (
    CHARS_PER_TOKEN,
    TIMEOUT_SECONDS,
    AIError,
    Exchange,
    send,
)

log = logging.getLogger(__name__)

CUT_OFF = "The answer hit the response token limit – raise it under Platform Settings."


@dataclass(frozen=True)
class AIAnswer:
    text: str
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    data: dict | None = None  # the parsed JSON object, when one was asked for


def ai_enabled(settings: Settings) -> bool:
    return bool(settings.ai_provider and settings.ai_model and settings.ai_base_url)


_THINKING = re.compile(r"<(think|thinking|reasoning)>(.*?)</\1>", re.S | re.I)
_OPENED = re.compile(r"<(think|thinking|reasoning)>", re.I)


def split_thinking(text: str) -> tuple[str, str]:
    """The answer without the reasoning some models write into it (<think>…</think>), and that
    reasoning. An unclosed block (cut off by the token limit) leaves no usable answer."""
    thoughts = [match.group(2).strip() for match in _THINKING.finditer(text)]
    text = _THINKING.sub("", text)
    opened = _OPENED.search(text)
    if opened:
        thoughts.append(text[opened.end() :].strip())
        text = text[: opened.start()]
    return text, "\n\n".join(t for t in thoughts if t)


def strip_thinking(text: str) -> str:
    """Without the reasoning, so it never ends up in an explanation or breaks a JSON draft."""
    return split_thinking(text)[0]


def parse_json(text: str) -> dict:
    """A JSON object from the answer (code fences around it are tolerated)."""
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AIError("The model did not answer with JSON.") from None
    if not isinstance(data, dict):
        raise AIError("The model did not answer with a JSON object.")
    return data


def complete(
    settings: Settings,
    system: str,
    user: str,
    *,
    schema: dict | None = None,
    usage: UsageLog | None = None,
) -> AIAnswer:
    """The model's answer to one request. `schema`: ask for JSON of this shape (parsed into
    `data`; its content is still checked by the caller). `usage`: log the request."""
    started = time.monotonic()
    exchange = Exchange(system, user)
    try:
        answer = _ask(settings, exchange, schema, started)
    except AIError as exc:
        record(settings, usage, exchange, started, exc)
        raise
    record(settings, usage, exchange, started)
    return answer


def _ask(settings: Settings, exchange: Exchange, schema: dict | None, started) -> AIAnswer:
    if not ai_enabled(settings):
        raise AIError("AI is not set up (Platform Settings → AI).")
    if (len(exchange.system) + len(exchange.user)) / CHARS_PER_TOKEN > settings.ai_max_input_tokens:
        raise AIError("The request is longer than the token limit set under Platform Settings.")
    try:
        reply = send(settings, exchange, schema)
    except httpx.TimeoutException:
        raise AIError(f"No answer within {TIMEOUT_SECONDS} seconds.") from None
    except httpx.HTTPError as exc:
        raise AIError(f"No connection to the provider ({type(exc).__name__}).") from None
    exchange.reply = reply
    seconds = time.monotonic() - started
    log.info("AI answer from %s/%s in %.1f s", settings.ai_provider, settings.ai_model, seconds)
    text, written = split_thinking(reply.text)
    exchange.thinking = "\n\n".join(t for t in (reply.thinking, written) if t)
    if reply.refused:
        raise AIError("The model declined to answer.")
    if reply.cut_off:
        raise AIError(CUT_OFF)
    text = text.strip()
    if not text:
        raise AIError("The model returned an empty answer.")
    data = parse_json(text) if schema is not None else None
    exchange.result = json.dumps(data, indent=2, ensure_ascii=False) if data is not None else text
    return AIAnswer(text, seconds, reply.input_tokens, reply.output_tokens, data)


def complete_json(
    settings: Settings, system: str, user: str, schema: dict, *, usage: UsageLog | None = None
) -> dict:
    """Like `complete`, the answer as a JSON object; an answer that is none counts as failed."""
    return complete(settings, system, user, schema=schema, usage=usage).data or {}
