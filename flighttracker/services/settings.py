"""Admin-editable overrides of selected `.env` defaults, stored in `platform_settings`.

Only the fields listed in `SETTING_FIELDS` can ever be overridden; everything else (secrets,
database URL, timezone, …) only ever comes from the environment. An override is applied on
top of the process' `Settings` – it takes effect immediately for the request/worker tick that
reads it, no restart needed.
"""

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.models import PlatformSetting

Kind = Literal["str", "int", "float", "secret", "currency"]

# Sections shown on the Platform Settings page, in this order.
PROVIDERS_SECTION = "providers"
SEARCHES_SECTION = "searches"
WORKER_SECTION = "worker"
AIRPORT_IMPORT_SECTION = "airport_import"
USERS_SECTION = "users"


@dataclass(frozen=True)
class SettingField:
    key: str
    section: str
    kind: Kind
    min: float | None = None
    max: float | None = None


SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("flight_provider", PROVIDERS_SECTION, "str"),
    SettingField("travelpayouts_token", PROVIDERS_SECTION, "secret"),
    SettingField("scraper_days_per_month", PROVIDERS_SECTION, "int", min=1, max=31),
    SettingField("scraper_request_delay_seconds", PROVIDERS_SECTION, "float", min=0, max=60),
    SettingField("default_currency", SEARCHES_SECTION, "currency"),
    SettingField("default_poll_interval_minutes", SEARCHES_SECTION, "int", min=15, max=43200),
    SettingField("worker_tick_seconds", WORKER_SECTION, "int", min=1, max=3600),
    SettingField("max_route_pairs_per_search", WORKER_SECTION, "int", min=1),
    SettingField("country_default_airports", AIRPORT_IMPORT_SECTION, "int", min=1, max=20),
    SettingField("wikidata_contact", AIRPORT_IMPORT_SECTION, "str"),
    SettingField("max_searches_per_user", USERS_SECTION, "int", min=0, max=1000),
    SettingField("max_requests_per_user", USERS_SECTION, "int", min=0, max=1_000_000),
)

_FIELDS_BY_KEY = {field.key: field for field in SETTING_FIELDS}


class SettingValidationError(ValueError):
    pass


def _parse(field: SettingField, raw: str) -> object:
    raw = raw.strip()
    if field.kind == "secret":
        return raw
    if field.kind == "currency":
        if len(raw) != 3 or not raw.isalpha() or not raw.isupper():
            raise SettingValidationError("Enter a three-letter uppercase currency code.")
        return raw
    if field.kind == "str":
        return raw
    try:
        value: float = int(raw) if field.kind == "int" else float(raw)
    except ValueError as exc:
        raise SettingValidationError("Please enter a number.") from exc
    if not math.isfinite(value):
        raise SettingValidationError("Please enter a number.")
    if field.min is not None and value < field.min:
        raise SettingValidationError(f"Must be at least {field.min:g}.")
    if field.max is not None and value > field.max:
        raise SettingValidationError(f"Must be at most {field.max:g}.")
    return int(value) if field.kind == "int" else value


def validate(key: str, raw: str) -> object:
    """Parsed, bounds-checked value, or raises `SettingValidationError`."""
    field = _FIELDS_BY_KEY.get(key)
    if field is None:
        raise SettingValidationError(f"Unknown setting: {key}")
    if key == "flight_provider":
        from flighttracker.providers.registry import PROVIDERS

        if raw not in PROVIDERS:
            available = ", ".join(sorted(PROVIDERS))
            raise SettingValidationError(f"Unknown provider. Available: {available}")
        return raw
    return _parse(field, raw)


def load_overrides(session: Session) -> dict[str, str]:
    rows = session.scalars(select(PlatformSetting))
    return {row.key: row.value for row in rows}


def set_override(session: Session, key: str, raw: str) -> None:
    """Validates and stores one override; an empty value clears it (falls back to `.env`)."""
    if key not in _FIELDS_BY_KEY:
        raise SettingValidationError(f"Unknown setting: {key}")
    if not raw.strip():
        clear_override(session, key)
        return
    validate(key, raw)  # raises on invalid input, nothing is stored
    statement = insert(PlatformSetting).values(key=key, value=raw.strip())
    statement = statement.on_conflict_do_update(
        index_elements=[PlatformSetting.key], set_={"value": statement.excluded.value}
    )
    session.execute(statement)


def clear_override(session: Session, key: str) -> None:
    session.query(PlatformSetting).filter(PlatformSetting.key == key).delete()


def effective_settings(session: Session, base: Settings) -> Settings:
    """`base` (from `.env`) with every stored override applied."""
    overrides = load_overrides(session)
    if not overrides:
        return base
    update: dict[str, object] = {}
    for key, raw in overrides.items():
        field = _FIELDS_BY_KEY.get(key)
        if field is None:
            continue
        try:
            value = validate(key, raw)
        except SettingValidationError:
            continue  # a stored value that no longer validates is ignored, not fatal
        if field.kind == "secret":
            update[key] = SecretStr(value) if value else None
        else:
            update[key] = value
    return base.model_copy(update=update) if update else base
