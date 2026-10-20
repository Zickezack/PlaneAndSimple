"""Admin-editable overrides of selected `.env` defaults, stored in `platform_settings`.

Only the fields listed in `SETTING_FIELDS` can ever be overridden; everything else (secrets,
database URL, timezone, …) only ever comes from the environment. An override is applied on
top of the process' `Settings` – it takes effect immediately for the request/worker tick that
reads it, no restart needed.
"""

import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flighttracker.config import Settings
from flighttracker.domain.currencies import is_known_currency
from flighttracker.domain.emails import is_valid_email
from flighttracker.i18n import Msg
from flighttracker.models import PlatformSetting
from flighttracker.security.secrets import decrypt, encrypt

Kind = Literal[
    "str", "text", "int", "float", "bool", "secret", "currency", "email", "url", "choice"
]

# Sections shown on the Platform Settings page, in this order.
PROVIDERS_SECTION = "providers"
SEARCHES_SECTION = "searches"
WORKER_SECTION = "worker"
AIRPORT_IMPORT_SECTION = "airport_import"
USERS_SECTION = "users"
MAIL_SECTION = "mail"
AI_SECTION = "ai"

# Allowed values of "choice" settings ("" = off where listed).
CHOICES = {
    "smtp_security": ("starttls", "ssl", "none"),
    "ai_provider": ("", "ollama", "openai", "anthropic"),
}


@dataclass(frozen=True)
class SettingField:
    key: str
    section: str
    kind: Kind
    min: float | None = None
    max: float | None = None
    # Secrets stored encrypted with a key from SECRET_KEY (security/secrets.py).
    encrypted: bool = False


SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("flight_provider", PROVIDERS_SECTION, "str"),
    SettingField("travelpayouts_token", PROVIDERS_SECTION, "secret"),
    SettingField("scraper_days_per_month", PROVIDERS_SECTION, "int", min=1, max=31),
    SettingField("scraper_request_delay_seconds", PROVIDERS_SECTION, "float", min=0, max=60),
    SettingField("scraper_return_flights", PROVIDERS_SECTION, "bool"),
    SettingField("default_currency", SEARCHES_SECTION, "currency"),
    SettingField("default_poll_interval_minutes", SEARCHES_SECTION, "int", min=15, max=43200),
    SettingField("worker_tick_seconds", WORKER_SECTION, "int", min=1, max=3600),
    SettingField("max_route_pairs_per_search", WORKER_SECTION, "int", min=1),
    SettingField("country_default_airports", AIRPORT_IMPORT_SECTION, "int", min=1, max=20),
    SettingField("wikidata_contact", AIRPORT_IMPORT_SECTION, "str"),
    SettingField("max_searches_per_user", USERS_SECTION, "int", min=0, max=1000),
    SettingField("max_requests_per_user", USERS_SECTION, "int", min=0, max=1_000_000),
    SettingField("public_base_url", MAIL_SECTION, "url"),
    SettingField("smtp_host", MAIL_SECTION, "str"),
    SettingField("smtp_port", MAIL_SECTION, "int", min=1, max=65535),
    SettingField("smtp_security", MAIL_SECTION, "choice"),
    SettingField("smtp_username", MAIL_SECTION, "str"),
    SettingField("smtp_password", MAIL_SECTION, "secret", encrypted=True),
    SettingField("mail_from", MAIL_SECTION, "email"),
    SettingField("ai_provider", AI_SECTION, "choice"),
    SettingField("ai_base_url", AI_SECTION, "url"),
    SettingField("ai_model", AI_SECTION, "str"),
    SettingField("ai_ollama_num_ctx", AI_SECTION, "int", min=0, max=1_048_576),
    SettingField("ai_ollama_keep_alive", AI_SECTION, "str"),
    SettingField("ai_ollama_no_thinking", AI_SECTION, "bool"),
    SettingField("ai_api_key", AI_SECTION, "secret", encrypted=True),
    SettingField("ai_max_input_tokens", AI_SECTION, "int", min=1000, max=2_000_000),
    SettingField("ai_max_output_tokens", AI_SECTION, "int", min=256, max=128_000),
    SettingField("ai_system_prompt", AI_SECTION, "text"),
    SettingField("ai_drafts", AI_SECTION, "bool"),
    SettingField("ai_explanations", AI_SECTION, "bool"),
    SettingField("ai_poll_summaries", AI_SECTION, "bool"),
    SettingField("ai_log_messages", AI_SECTION, "bool"),
)

_FIELDS_BY_KEY = {field.key: field for field in SETTING_FIELDS}


KEEP_ALIVE = re.compile(r"^(-1|\d+(\.\d+)?(ms|s|m|h)?)$")


class SettingValidationError(ValueError):
    """`message` is translatable (shown next to the field in the user's language)."""

    def __init__(self, message: Msg):
        super().__init__(str(message))
        self.message = message


def _parse(field: SettingField, raw: str) -> object:
    raw = raw.strip()
    if field.kind == "secret":
        return raw
    if field.kind == "currency":
        if not is_known_currency(raw):
            raise SettingValidationError(Msg("Choose a currency from the list."))
        return raw
    if field.kind == "str":
        return raw
    if field.kind == "text":
        if len(raw) > 20_000:
            raise SettingValidationError(Msg("At most {n} characters.", n=20_000))
        return raw
    if field.kind == "email":
        if not is_valid_email(raw):
            raise SettingValidationError(Msg("Enter a valid e-mail address."))
        return raw
    if field.kind == "url":
        if not raw.startswith(("https://", "http://")) or " " in raw:
            raise SettingValidationError(Msg("Enter an address starting with https://."))
        return raw.rstrip("/")
    if field.kind == "choice":
        if raw not in CHOICES[field.key]:
            raise SettingValidationError(
                Msg(
                    "Choose one of: {values}.",
                    values=", ".join(v or "–" for v in CHOICES[field.key]),
                )
            )
        return raw
    if field.kind == "bool":
        if raw.lower() not in ("true", "false"):
            raise SettingValidationError(Msg("Choose on or off."))
        return raw.lower() == "true"
    try:
        value: float = int(raw) if field.kind == "int" else float(raw)
    except ValueError as exc:
        raise SettingValidationError(Msg("Please enter a number.")) from exc
    if not math.isfinite(value):
        raise SettingValidationError(Msg("Please enter a number."))
    if field.min is not None and value < field.min:
        raise SettingValidationError(Msg("Must be at least {n}.", n=f"{field.min:g}"))
    if field.max is not None and value > field.max:
        raise SettingValidationError(Msg("Must be at most {n}.", n=f"{field.max:g}"))
    return int(value) if field.kind == "int" else value


def validate(key: str, raw: str) -> object:
    """Parsed, bounds-checked value, or raises `SettingValidationError`."""
    field = _FIELDS_BY_KEY.get(key)
    if field is None:
        raise SettingValidationError(Msg("Unknown setting: {key}", key=key))
    if key == "ai_ollama_keep_alive" and not KEEP_ALIVE.match(raw.strip()):
        raise SettingValidationError(Msg("Use -1 (forever), 0, or a duration such as 30m or 24h."))
    if key == "flight_provider":
        from flighttracker.providers.registry import PROVIDERS

        if raw not in PROVIDERS:
            available = ", ".join(sorted(PROVIDERS))
            raise SettingValidationError(
                Msg("Unknown provider. Available: {names}", names=available)
            )
        return raw
    return _parse(field, raw)


def load_overrides(session: Session) -> dict[str, str]:
    rows = session.scalars(select(PlatformSetting))
    return {row.key: row.value for row in rows}


def set_override(session: Session, key: str, raw: str, *, secret_key: str | None = None) -> None:
    """Validates and stores one override; an empty value clears it (falls back to `.env`).
    `secret_key`: needed for encrypted settings (the SMTP password)."""
    field = _FIELDS_BY_KEY.get(key)
    if field is None:
        raise SettingValidationError(Msg("Unknown setting: {key}", key=key))
    if not raw.strip():
        clear_override(session, key)
        return
    value = validate(key, raw)  # raises on invalid input, nothing is stored
    stored = str(value).lower() if field.kind == "bool" else raw.strip()
    if field.kind in ("url", "choice", "email"):
        stored = str(value)
    if field.encrypted:
        if not secret_key:
            raise SettingValidationError(Msg("Encrypted settings need the secret key."))
        stored = encrypt(raw, secret_key)
    statement = insert(PlatformSetting).values(key=key, value=stored)
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
        if field.encrypted:
            value = decrypt(value, base.secret_key.get_secret_value())
            if value is None:
                continue  # stored with another SECRET_KEY: unusable, `.env` applies
        if field.kind == "secret":
            update[key] = SecretStr(value) if value else None
        else:
            update[key] = value
    return base.model_copy(update=update) if update else base
