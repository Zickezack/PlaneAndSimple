"""What the app asks the AI: a tracked-search draft from a plain-language wish, and the
"when to book" advice in sentences. The instructions are editable (Platform Settings → AI); the
guardrails below are always added – numbers come only from the app.
"""

import re
from datetime import date

from flighttracker.config import Settings
from flighttracker.domain.booking_advice import BookingAdvice
from flighttracker.domain.filters import CabinClass
from flighttracker.services import ai

DEFAULT_SYSTEM_PROMPT = """\
You are the assistant of "Plane and simple", a self-hosted flight price tracker.
Users track flight prices ("tracked searches") between airports or whole countries; the app
polls Google Flights regularly and keeps every price it sees.

Write like a knowledgeable, friendly travel agent: short, concrete, no marketing language,
no exclamation marks. Prefer plain sentences over lists. Never claim certainty about future
prices – explain what the observed prices suggest."""

GUARDRAILS = """\
Rules that always apply:
- Use only the facts given in the request. Never invent prices, dates, airports, airlines or
  other numbers; if something is not given, do not mention it.
- Repeat numbers exactly as given, with their currency.
- Airports are IATA codes (3 letters), countries ISO 3166 codes (2 letters)."""

LANGUAGES = {"en": "English", "de": "German"}
CODE = re.compile(r"^[A-Z]{2,3}$")


def system_prompt(settings: Settings, locale: str) -> str:
    instructions = settings.ai_system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    language = LANGUAGES.get(locale, "English")
    return f"{instructions}\n\n{GUARDRAILS}\n- Answer in {language}."


# --- Draft of a tracked search -----------------------------------------------------------------

_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "origins": {"type": "array", "items": {"type": "string"}},
        "destinations": {"type": "array", "items": {"type": "string"}},
        "trip_type": {"type": "string", "enum": ["round_trip", "one_way"]},
        "cabin_classes": {
            "type": "array",
            "items": {"type": "string", "enum": [c.value for c in CabinClass]},
        },
        "max_stops": {"type": "string", "enum": ["any", "0", "1", "2"]},
        "months_ahead": {"type": "integer"},
        "stay_days_min": _NULLABLE_INT,
        "stay_days_max": _NULLABLE_INT,
        "adults": {"type": "integer"},
        "children": {"type": "integer"},
        "notes": {"type": "string"},
    },
    "required": [
        "name",
        "origins",
        "destinations",
        "trip_type",
        "cabin_classes",
        "max_stops",
        "months_ahead",
        "stay_days_min",
        "stay_days_max",
        "adults",
        "children",
        "notes",
    ],
    "additionalProperties": False,
}

DRAFT_TASK = """\
Turn the travel wish below into a draft of a tracked search. Today is {today}.

Fields (answer with one JSON object, nothing else):
- name: short name for the search, e.g. "Zurich → Lisbon, autumn".
- origins / destinations: IATA airport codes, or an ISO country code for "anywhere in <country>".
  Use the airports a traveller would really use; at most 4 each.
- trip_type: "round_trip" or "one_way".
- cabin_classes: one or more of economy, premium_economy, business, first (default economy).
- max_stops: "any", "0" (direct only), "1" or "2".
- months_ahead: how many months from the current month to track, 1–12 (cover the travel period).
- stay_days_min / stay_days_max: length of stay in days for round trips, else null.
- adults (1–9) and children aged 2–11 (0–8); default 1 adult, 0 children.
- notes: one sentence in the answer language on what you assumed or could not express
  (e.g. exact dates – the app tracks whole months).

Travel wish:
{wish}"""


def draft_search(
    settings: Settings, wish: str, today: date, locale: str, *, usage: ai.UsageLog | None = None
) -> dict:
    """The model's draft (JSON object of `DRAFT_SCHEMA`); `draft_values` makes form values of it."""
    task = DRAFT_TASK.format(today=today.isoformat(), wish=wish.strip()[:2000])
    return ai.complete_json(
        settings, system_prompt(settings, locale), task, DRAFT_SCHEMA, usage=usage
    )


def _int(value: object, low: int, high: int, default: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return min(max(int(value), low), high)


def draft_values(draft: dict, defaults: dict) -> tuple[dict, dict[str, list[str]]]:
    """Form values of the search form from a draft, plus per field ("origins", "destinations")
    the place names that are no codes (the caller looks them up). Everything is still
    validated when the user saves the form."""
    values = dict(defaults)
    unresolved: dict[str, list[str]] = {"origins": [], "destinations": []}

    def places(key: str) -> str:
        codes = []
        for raw in draft.get(key) or []:
            text = str(raw).strip()
            if CODE.match(text.upper()) and len(text) <= 3:
                codes.append(text.upper())
            elif text:
                unresolved[key].append(text)
        return ", ".join(dict.fromkeys(codes))

    values["name"] = str(draft.get("name") or "")[:120]
    values["origins"] = places("origins")
    values["destinations"] = places("destinations")
    if draft.get("trip_type") in ("round_trip", "one_way"):
        values["trip_type"] = draft["trip_type"]
    cabins = [c for c in draft.get("cabin_classes") or [] if c in {c.value for c in CabinClass}]
    values["cabin_classes"] = cabins or defaults["cabin_classes"]
    if str(draft.get("max_stops")) in ("any", "0", "1", "2"):
        values["max_stops"] = str(draft["max_stops"])
    values["months_ahead"] = str(_int(draft.get("months_ahead"), 1, 12, 6))
    round_trip = values["trip_type"] == "round_trip"
    for key in ("stay_days_min", "stay_days_max"):
        stay = _int(draft.get(key), 1, 60, None) if round_trip else None
        values[key] = str(stay) if stay is not None else ""
    values["adults"] = str(_int(draft.get("adults"), 1, 9, 1))
    values["children"] = str(_int(draft.get("children"), 0, 8, 0))
    return values, unresolved


# --- "When to book" in sentences ----------------------------------------------------------------

EXPLAIN_TASK = """\
Explain in 2–4 sentences whether now looks like a good moment to book this flight and why.
Base it on the facts and the rule-based verdict below; you may weigh the signals, but do not
contradict the numbers. End with a short reminder that past prices do not guarantee future ones.

Flight: {route}, {cabin}, departure {departure}{returning}, {passengers}
Days until departure: {days}
Observed prices ({count} polls, oldest first): {prices}
First observed: {first} · lowest: {lowest} · highest: {highest} · average: {average}
Current price: {current}
Rule-based signals: {signals}
Rule-based verdict: {verdict}"""

VERDICT_TEXT = {
    "book": "a good moment to book",
    "wait": "likely to get cheaper – wait",
    "watch": "nothing clear yet – keep watching",
}


def explain_advice(
    settings: Settings,
    advice: BookingAdvice,
    *,
    route: str,
    cabin: str,
    departure: date,
    return_date: date | None,
    passengers: int,
    prices: list[str],
    money,
    locale: str,
    usage: ai.UsageLog | None = None,
) -> ai.AIAnswer:
    """`prices`: the observed prices already formatted; `money`: formats one amount."""
    task = EXPLAIN_TASK.format(
        route=route,
        cabin=cabin,
        departure=departure.isoformat(),
        returning=f", return {return_date.isoformat()}" if return_date else "",
        passengers=f"{passengers} passenger" + ("s" if passengers != 1 else ""),
        days=advice.days_to_departure,
        count=advice.observations,
        prices=", ".join(prices[-30:]),
        first=money(advice.first),
        lowest=money(advice.lowest),
        highest=money(advice.highest),
        average=money(advice.average),
        current=money(advice.current),
        signals=", ".join(advice.signals) or "none",
        verdict=VERDICT_TEXT[advice.verdict],
    )
    return ai.complete(settings, system_prompt(settings, locale), task, usage=usage)


def connection_test(settings: Settings, usage: ai.UsageLog | None = None) -> ai.AIAnswer:
    return ai.complete(settings, "Answer with exactly one word.", "Reply with: OK", usage=usage)
