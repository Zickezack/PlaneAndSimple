"""AI provider presets for Platform Settings → AI: Ollama (its own API), Anthropic's Messages
API, and the OpenAI-compatible chat completions API (OpenAI itself, LM Studio, Mistral, Groq,
OpenRouter, …)."""

from dataclasses import dataclass

ANTHROPIC = "anthropic"
OPENAI = "openai"  # any OpenAI-compatible endpoint
OLLAMA = "ollama"  # OpenAI-compatible, local, usually without key
PROVIDERS = (OLLAMA, OPENAI, ANTHROPIC)


@dataclass(frozen=True)
class AIPreset:
    key: str
    label: str
    base_url: str
    model: str
    hint: str
    # Data leaves the server: shown next to the provider.
    cloud: bool


AI_PRESETS: tuple[AIPreset, ...] = (
    AIPreset(
        OLLAMA,
        "Ollama (local)",
        "http://host.docker.internal:11434",
        "llama3.2",
        "Runs on your own machine; nothing leaves it. Use the model name from `ollama list`; "
        "from inside Docker the host is host.docker.internal (or the Ollama container's name). "
        "If other apps share the model, set the same context window and keep-alive below.",
        cloud=False,
    ),
    AIPreset(
        OPENAI,
        "OpenAI-compatible",
        "https://api.openai.com/v1",
        "",
        "OpenAI, Mistral, Groq, OpenRouter, LM Studio, … – the provider's base URL ending in /v1 "
        "and a model ID from its model list; an API key from the provider's console.",
        cloud=True,
    ),
    AIPreset(
        ANTHROPIC,
        "Anthropic (Claude)",
        "https://api.anthropic.com",
        "claude-opus-5-5",
        "An API key from console.anthropic.com. Claude Opus 5.5 is the default; a cheaper model "
        "such as claude-haiku-5-5 also works for these short tasks.",
        cloud=True,
    ),
)

PRESETS_BY_KEY = {preset.key: preset for preset in AI_PRESETS}
