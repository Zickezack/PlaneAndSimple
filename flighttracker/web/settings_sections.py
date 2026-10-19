"""Platform Settings sections as forms: read and check what was entered, apply a provider preset
only to fields left untouched, build settings from the entered values (to test a mail server or
an AI provider before saving) and store them.

Every field is checked on its own, so one typo marks that field instead of throwing away the
whole input; nothing is stored while any field is wrong.
"""

from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.i18n import Msg
from flighttracker.services.settings import (
    SettingValidationError,
    clear_override,
    set_override,
    validate,
)
from flighttracker.web.settings_forms import ai_preset, mail_preset, section_fields

# Section → (name of its preset select, preset values).
PRESETS = {"mail": ("smtp_preset", mail_preset), "ai": ("ai_preset", ai_preset)}


@dataclass(frozen=True)
class SectionInput:
    section: str
    values: dict[str, str]  # as entered (a preset filled only untouched fields)
    clear: frozenset[str]  # secrets to clear (fall back to .env)
    errors: dict[str, Msg]  # field key → message (translated when shown)

    @property
    def ok(self) -> bool:
        return not self.errors


def as_text(value: object) -> str:
    """A current setting as the form shows it, to tell an untouched field from an edited one."""
    if value is None or isinstance(value, SecretStr):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def read_section(form: FormData, section: str, current: Settings) -> SectionInput:
    fields = section_fields(section)
    values = {field.key: str(form.get(field.key, "")).strip() for field in fields}
    if section in PRESETS:
        select, preset = PRESETS[section]
        for key, value in preset(str(form.get(select, ""))).items():
            # A preset only fills what the user left as it was – edits always win.
            untouched = values.get(key, "") in ("", as_text(getattr(current, key)))
            if value and untouched:
                values[key] = value
    clear = frozenset(
        field.key
        for field in fields
        if field.kind == "secret" and form.get(f"{field.key}_clear") == "1"
    )
    errors = {}
    for field in fields:
        raw = values[field.key]
        if not raw or field.key in clear:
            continue
        try:
            validate(field.key, raw)
        except SettingValidationError as exc:
            errors[field.key] = exc.message
    return SectionInput(section, values, clear, errors)


def draft_settings(base: Settings, current: Settings, entered: SectionInput) -> Settings:
    """`current` with the entered values of one section (only for a valid input) – what the
    settings would be after saving, to test them first. An empty field means `.env`; an empty
    secret keeps the stored one."""
    update: dict[str, object] = {}
    for field in section_fields(entered.section):
        raw = entered.values[field.key]
        if field.kind == "secret":
            if field.key in entered.clear:
                update[field.key] = getattr(base, field.key)
            elif raw:
                update[field.key] = SecretStr(raw)
            continue
        update[field.key] = validate(field.key, raw) if raw else getattr(base, field.key)
    return current.model_copy(update=update)


def store_section(session: Session, entered: SectionInput, secret_key: str) -> None:
    """Stores a valid input (the caller commits). Empty fields fall back to `.env`; an empty
    secret keeps the stored one unless "Clear" was ticked."""
    for field in section_fields(entered.section):
        if field.key in entered.clear:
            clear_override(session, field.key)
            continue
        raw = entered.values[field.key]
        if field.kind == "secret" and not raw:
            continue
        set_override(session, field.key, raw, secret_key=secret_key)
