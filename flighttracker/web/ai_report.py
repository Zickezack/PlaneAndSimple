"""What the AI connection test says about Ollama: how it holds the model (context window,
until when) and whether the settings here would make it reload or unload the model."""

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.services import ai


def _held(found) -> Msg:
    # Ollama reports "forever" (keep_alive -1) as an expiry centuries ahead.
    if not found.expires_at or found.expires_at[:4] > "2200":
        return Msg(
            "Ollama holds {model} with a context window of {n} tokens "
            "until it is unloaded by hand.",
            model=found.name,
            n=found.context_length,
        )
    return Msg(
        "Ollama holds {model} with a context window of {n} tokens until {time}.",
        model=found.name,
        n=found.context_length,
        time=found.expires_at[:16].replace("T", " "),
    )


def ollama_report(settings: Settings) -> list[tuple[Msg, str]]:
    """How Ollama holds the model after the test, and whether our settings would reload it."""
    model = settings.ai_model
    loaded = [m for m in ai.ollama_loaded(settings) if m.name in (model, f"{model}:latest")]
    report: list[tuple[Msg, str]] = []
    if not loaded:
        return [(Msg("Ollama did not report {model} as loaded.", model=model), "info")]
    found = loaded[0]
    if found.context_length:
        report.append((_held(found), "info"))
    configured = settings.ai_ollama_num_ctx
    if configured and found.context_length and configured != found.context_length:
        report.append(
            (
                Msg(
                    "The context window set here ({configured}) differs from the loaded model's "
                    "({loaded}): requests from here and from other apps make Ollama reload it.",
                    configured=configured,
                    loaded=found.context_length,
                ),
                "error",
            )
        )
    if not configured:
        report.append(
            (
                Msg("No context window set: Ollama uses its default and may reload the model."),
                "info",
            )
        )
    if not settings.ai_ollama_keep_alive:
        report.append(
            (
                Msg(
                    "No keep-alive set: every request resets the model's lifetime to Ollama's "
                    "default (5 minutes)."
                ),
                "info",
            )
        )
    return report
