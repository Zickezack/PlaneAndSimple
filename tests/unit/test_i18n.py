import ast
import re
import string
from pathlib import Path

import pytest

from flighttracker.domain.currencies import CURRENCIES
from flighttracker.i18n import Msg, catalog, translate
from flighttracker.web import forms, labels

ROOT = Path(__file__).resolve().parents[2] / "flighttracker"
_TEMPLATE_CALL = re.compile(r"""_\(\s*(["'])(.*?)(?<!\\)\1""", re.S)
# Texts coming from pydantic validators in the domain (raised as ValueError, wrapped in Msg).
VALIDATOR_TEXTS = {
    "A length of stay is only possible for round trips.",
    "The minimum stay is longer than the maximum stay.",
    "At most 9 passengers in total.",
    "A trip must contain between 2 and 8 legs.",
    "Choose between 1 and 9 adults.",
    "Enter a three-letter uppercase currency code.",
    "Enter a trip name of at most 100 characters.",
    "Every leg must start at an airport served by the previous leg.",
    "Every leg needs both an origin and a destination airport.",
    "Maximum layover must be between 1 and 60 days.",
    "Maximum layover cannot be shorter than minimum layover.",
    "Minimum layover must be between 1 and 60 days.",
    "Choose between 0 and 8 children.",
    "Origin and destination must differ for every leg.",
    "Select at least one cabin class.",
    "The end date must be on or after the start date.",
    "The trip search window must start today or later.",
    "The trip window cannot exceed 60 days.",
    "The Trip Planner needs a provider with flight times; select Google Flights in Platform Settings.",
    "Trip polls must be at least 15 minutes apart.",
    "Use airport codes or two-letter country codes for every leg.",
}


def ui_texts() -> set[str]:
    """Every English source text of the UI: templates, Msg/translate calls, label tables."""
    texts = set(VALIDATOR_TEXTS)
    for path in (ROOT / "web" / "templates").rglob("*.html"):
        texts.update(m.group(2) for m in _TEMPLATE_CALL.finditer(path.read_text()))
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                in ("Msg", "translate")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                texts.add(node.args[0].value)
    for table in (
        labels.CABIN_LABELS,
        labels.TRIP_TYPE_LABELS,
        labels.STATUS_LABELS,
        labels.LOCATION_KIND_LABELS,
        labels.JOB_KIND_LABELS,
        labels.JOB_STATUS_LABELS,
        labels.OUTCOME_LABELS,
        labels.CHART_LABELS,
        labels.DAYS_PER_MONTH_LABELS,
        labels.SETTINGS_SECTION_LABELS,
        labels.SETTINGS_FIELD_LABELS,
        labels.SETTINGS_FIELD_HINTS,
        forms._FIELD_LABELS,
        CURRENCIES,
    ):
        texts.update(table.values())
    texts.update(text for _, text in labels.STOPS_OPTIONS)
    return texts


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_ui_text_has_a_german_translation():
    missing = sorted(ui_texts() - catalog("de").keys())
    assert missing == []


def test_german_catalog_has_no_stale_entries():
    assert sorted(catalog("de").keys() - ui_texts()) == []


@pytest.mark.parametrize("text", sorted(catalog("de")))
def test_translations_keep_placeholders(text):
    assert placeholders(catalog("de")[text]) == placeholders(text)


def test_translate_fills_params_and_falls_back_to_english():
    assert translate("Revision {no}", "de", no=3) == "Revision 3"
    assert translate("Log in", "de") == "Anmelden"
    assert translate("Not in any catalog", "de") == "Not in any catalog"
    assert translate("Log in", "fr") == "Log in"


def test_nested_messages_and_session_roundtrip():
    message = Msg("Origin: {error}", error=Msg('Unknown airport "{code}".', code="XXX"))
    assert message.render("en") == 'Origin: Unknown airport "XXX".'
    assert message.render("de") == "Abflug: Unbekannter Flughafen „XXX“."
    simple = Msg('Tracked search "{name}" created. The first poll starts shortly.', name="A")
    assert Msg.from_json(simple.to_json()) == simple
