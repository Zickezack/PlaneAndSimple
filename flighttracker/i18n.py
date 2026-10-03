"""UI languages: English (default and source language of every text) and German.

All texts are written in English in the code. `translate` looks them up in the catalog of
the requested language (`flighttracker/locales/<locale>.json`, keyed by the English text).
Messages created below the web layer (validation errors, flash messages) are `Msg` objects,
so each request can render them in its own language. Pure module: usable from every layer.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = {"en": "English", "de": "Deutsch"}
LOCALE_DIR = Path(__file__).parent / "locales"


@cache
def catalog(locale: str) -> dict[str, str]:
    path = LOCALE_DIR / f"{locale}.json"
    if locale == DEFAULT_LOCALE or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def translate(text: str, locale: str, **params) -> str:
    """Translated text with `{name}` placeholders filled; unknown texts stay English."""
    template = catalog(locale).get(text, text)
    if not params:
        return template
    rendered = {k: v.render(locale) if isinstance(v, Msg) else v for k, v in params.items()}
    return template.format(**rendered)


@dataclass(frozen=True)
class Msg:
    """A translatable message with parameters, rendered later in the viewer's language."""

    text: str
    params: Mapping[str, object] = field(default_factory=dict)

    def __init__(self, text: str, **params):
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "params", params)

    def render(self, locale: str) -> str:
        return translate(self.text, locale, **self.params)

    def to_json(self) -> dict:
        """For the session cookie – parameters must be plain strings or numbers."""
        return {"text": self.text, "params": dict(self.params)}

    @classmethod
    def from_json(cls, data: dict) -> "Msg":
        return cls(data["text"], **data.get("params", {}))

    def __str__(self) -> str:
        return self.render(DEFAULT_LOCALE)
