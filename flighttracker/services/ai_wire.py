"""The AI providers' wire formats over plain HTTP (httpx, no provider SDK).

Anthropic's Messages API, Ollama's native chat API (it alone takes the context window and
keep-alive – sent on every request so a model shared with other platforms is never reloaded or
unloaded early) and the OpenAI-compatible chat completions API (OpenAI, LM Studio, Mistral,
Groq, OpenRouter …). `send` notes in the `Exchange` what goes out (address and parameters, never
a header – the key travels there) and returns what came back as a `Reply`: the answer, thinking
the provider returns separately, the stop reason, tokens and the rest of the provider's response.
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from flighttracker.config import Settings
from flighttracker.domain.ai_providers import ANTHROPIC, OLLAMA

TIMEOUT_SECONDS = 90  # local models can be slow; the web request waits for the answer
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_URL = "https://api.anthropic.com"
# Rough size of a token, to keep requests below the configured limit without a tokenizer.
CHARS_PER_TOKEN = 3.5


class AIError(Exception):
    """The provider could not answer; the message is safe to show (never contains the key).
    `received`: the provider's error response, for the usage log."""

    def __init__(self, message: str, received: str | None = None):
        super().__init__(message)
        self.received = received


@dataclass(frozen=True)
class Reply:
    text: str  # the answer as received (reasoning written into it still included)
    thinking: str = ""  # reasoning the provider returned separately
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop: str | None = None  # the provider's stop/finish/done reason
    cut_off: bool = False  # the answer hit the response token limit
    refused: bool = False
    details: dict = field(default_factory=dict)  # the response without the texts


@dataclass
class Exchange:
    """One request for the usage log: filled in step by step, so a failed one is complete too."""

    system: str
    user: str
    url: str = ""
    params: dict = field(default_factory=dict)  # the request body without the prompts
    output_limit: int | None = None  # tokens the answer may use
    reply: Reply | None = None
    thinking: str = ""  # separately returned and written into the answer
    result: str | None = None  # the answer as the app used it


def _key(settings: Settings) -> str:
    return settings.ai_api_key.get_secret_value() if settings.ai_api_key else ""


def _shown(url: str) -> str:
    """The address without credentials or query, as the log shows it."""
    parts = urlsplit(url)
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _post(exchange: Exchange, url: str, body: dict, headers: dict | None = None) -> dict:
    exchange.url = _shown(url)
    exchange.params = {k: v for k, v in body.items() if k not in ("system", "messages")}
    response = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
    return read_json(response)


def _anthropic(settings: Settings, exchange: Exchange, schema: dict | None) -> Reply:
    body: dict = {
        "model": settings.ai_model,
        "max_tokens": settings.ai_max_output_tokens,
        "system": exchange.system,
        "messages": [{"role": "user", "content": exchange.user}],
    }
    if schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    exchange.output_limit = settings.ai_max_output_tokens
    base = (settings.ai_base_url or ANTHROPIC_URL).rstrip("/")
    headers = {"x-api-key": _key(settings), "anthropic-version": ANTHROPIC_VERSION}
    data = _post(exchange, f"{base}/v1/messages", body, headers)
    blocks = data.get("content") or []
    thinking = [
        b.get("thinking", "") if b.get("type") == "thinking" else "[redacted by the provider]"
        for b in blocks
        if b.get("type") in ("thinking", "redacted_thinking")
    ]
    stop = data.get("stop_reason")
    usage = data.get("usage") or {}
    return Reply(
        text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
        thinking="\n\n".join(t for t in thinking if t),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        stop=stop,
        cut_off=stop == "max_tokens",
        refused=stop == "refusal",
        details={k: v for k, v in data.items() if k != "content"}
        | {"content": [b.get("type") for b in blocks]},
    )


def _openai(settings: Settings, exchange: Exchange, schema: dict | None) -> Reply:
    body: dict = {
        "model": settings.ai_model,
        "max_tokens": settings.ai_max_output_tokens,
        "messages": [
            {"role": "system", "content": exchange.system},
            {"role": "user", "content": exchange.user},
        ],
    }
    if schema is not None:
        # The JSON mode every compatible server knows; the schema itself is in the prompt.
        body["response_format"] = {"type": "json_object"}
    exchange.output_limit = settings.ai_max_output_tokens
    headers = {"Authorization": f"Bearer {_key(settings)}"} if _key(settings) else {}
    data = _post(exchange, f"{settings.ai_base_url.rstrip('/')}/chat/completions", body, headers)
    try:
        choice = data["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise AIError("The provider's answer has an unexpected format.") from None
    usage = data.get("usage") or {}
    stop = choice.get("finish_reason")
    texts = ("content", "reasoning_content", "reasoning")
    rest = {k: v for k, v in message.items() if k not in texts}
    return Reply(
        text=text,
        # Reasoning as DeepSeek, vLLM and LM Studio (reasoning_content) or OpenRouter and Groq
        # (reasoning) return it.
        thinking=message.get("reasoning_content") or message.get("reasoning") or "",
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        stop=stop,
        cut_off=stop == "length",
        refused=stop == "content_filter",
        details=data | {"choices": [{**choice, "message": rest}]},
    )


def ollama_base(settings: Settings) -> str:
    """Ollama's own API lives at the server root (an OpenAI-style …/v1 URL is accepted too)."""
    return settings.ai_base_url.rstrip("/").removesuffix("/v1")


def _ollama(settings: Settings, exchange: Exchange, schema: dict | None) -> Reply:
    options: dict = {"num_predict": settings.ai_max_output_tokens}
    if settings.ai_ollama_num_ctx:
        # Same context window as the loaded model, otherwise Ollama reloads it for us.
        options["num_ctx"] = settings.ai_ollama_num_ctx
        prompt = int((len(exchange.system) + len(exchange.user)) / CHARS_PER_TOKEN)
        room = settings.ai_ollama_num_ctx - prompt
        if room < 256:
            raise AIError("The request does not fit into the Ollama context window.")
        options["num_predict"] = min(settings.ai_max_output_tokens, room)
    exchange.output_limit = options["num_predict"]
    body: dict = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": exchange.system},
            {"role": "user", "content": exchange.user},
        ],
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
    data = _post(exchange, f"{ollama_base(settings)}/api/chat", body)
    message = data.get("message") or {}
    stop = data.get("done_reason")
    return Reply(
        text=message.get("content") or "",
        thinking=message.get("thinking") or "",
        input_tokens=data.get("prompt_eval_count"),
        output_tokens=data.get("eval_count"),
        stop=stop,
        cut_off=stop == "length",
        # Durations (nanoseconds): total, model loading, reading the prompt, writing the answer.
        details=data
        | {"message": {k: v for k, v in message.items() if k not in ("content", "thinking")}},
    )


def send(settings: Settings, exchange: Exchange, schema: dict | None) -> Reply:
    """Sends the request in the provider's format; raises `AIError` or httpx errors."""
    call = {ANTHROPIC: _anthropic, OLLAMA: _ollama}.get(settings.ai_provider, _openai)
    return call(settings, exchange, schema)


@dataclass(frozen=True)
class LoadedModel:
    name: str
    context_length: int | None
    expires_at: str | None


def ollama_loaded(settings: Settings) -> list[LoadedModel]:
    """Models Ollama has in memory right now (`/api/ps`), for the connection test."""
    try:
        response = httpx.get(f"{ollama_base(settings)}/api/ps", timeout=10)
        models = read_json(response).get("models") or []
    except (httpx.HTTPError, AIError):
        return []
    return [
        LoadedModel(
            m.get("name") or m.get("model") or "?", m.get("context_length"), m.get("expires_at")
        )
        for m in models
    ]


def read_json(response: httpx.Response) -> dict:
    if response.status_code in (401, 403):
        # Not kept: some providers repeat part of the key in this answer.
        raise AIError(f"The provider rejected the API key (HTTP {response.status_code}).")
    received = response.text[:2000] if response.status_code >= 400 else None
    if response.status_code == 404:
        raise AIError("Model or API URL not found (HTTP 404) – check both.", received)
    if response.status_code == 429:
        raise AIError("The provider's rate or spending limit was reached (HTTP 429).", received)
    if response.status_code >= 400:
        raise AIError(f"The provider answered with HTTP {response.status_code}.", received)
    try:
        data = response.json()
    except ValueError:
        raise AIError("The provider's answer is not JSON.", response.text[:2000]) from None
    if not isinstance(data, dict):
        raise AIError("The provider's answer has an unexpected format.", response.text[:2000])
    return data
