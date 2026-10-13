"""Form helpers of the Settings page: Platform Settings sections and their fields, option
lists (currencies, time zones) and the SMTP provider presets."""

from zoneinfo import available_timezones

from flighttracker.domain.ai_providers import PRESETS_BY_KEY as AI_PRESETS_BY_KEY
from flighttracker.domain.currencies import CURRENCIES
from flighttracker.domain.mail_providers import PROVIDERS_BY_KEY
from flighttracker.i18n import currency_name, translate
from flighttracker.providers.registry import PROVIDERS
from flighttracker.services.settings import (
    AI_SECTION,
    AIRPORT_IMPORT_SECTION,
    CHOICES,
    MAIL_SECTION,
    PROVIDERS_SECTION,
    SEARCHES_SECTION,
    SETTING_FIELDS,
    USERS_SECTION,
    WORKER_SECTION,
    SettingField,
)
from flighttracker.web import labels

SECTIONS = (
    PROVIDERS_SECTION,
    SEARCHES_SECTION,
    WORKER_SECTION,
    AIRPORT_IMPORT_SECTION,
    USERS_SECTION,
    MAIL_SECTION,
    AI_SECTION,
)


def section_fields(section: str) -> list[SettingField]:
    return [field for field in SETTING_FIELDS if field.section == section]


def field_view(field: SettingField, overrides: dict, settings) -> dict:
    value = getattr(settings, field.key)
    is_secret = field.kind == "secret"
    return {
        "key": field.key,
        "label": labels.SETTINGS_FIELD_LABELS[field.key],
        "hint": labels.SETTINGS_FIELD_HINTS[field.key],
        "is_secret": is_secret,
        "is_choice": field.key == "flight_provider" or field.kind == "choice",
        "options": sorted(PROVIDERS) if field.key == "flight_provider" else CHOICES.get(field.key),
        "is_currency": field.kind == "currency",
        "is_bool": field.kind == "bool",
        "is_text": field.kind == "text",
        "is_overridden": field.key in overrides,
        "value": "" if is_secret else value,
        "has_value": bool(value.get_secret_value()) if is_secret and value else False,
    }


def ai_preset(key: str) -> dict[str, str]:
    """Base URL and model of a chosen AI provider preset (they win over the form fields)."""
    preset = AI_PRESETS_BY_KEY.get(key)
    if preset is None:
        return {}
    return {"ai_provider": preset.key, "ai_base_url": preset.base_url, "ai_model": preset.model}


def mail_preset(key: str) -> dict[str, str]:
    """Host, port and encryption of a chosen provider preset (they win over the form fields)."""
    preset = PROVIDERS_BY_KEY.get(key)
    if preset is None:
        return {}
    return {
        "smtp_host": preset.host,
        "smtp_port": str(preset.port),
        "smtp_security": preset.security,
    }


def currency_options(locale: str, current: str) -> list[tuple[str, str]]:
    """Every national currency as "CHF – Swiss Franc"; a legacy `.env` value stays selectable."""
    codes = sorted(CURRENCIES.keys() | {current})
    return [
        (code, f"{code} – {currency_name(code, CURRENCIES.get(code, code), locale)}")
        for code in codes
    ]


def timezone_options(platform_default: str, locale: str) -> list[tuple[str, str]]:
    """Empty value = the platform's zone; then every IANA zone of a region ("Europe/Zurich")."""
    zones = sorted(zone for zone in available_timezones() if "/" in zone and "Etc/" not in zone)
    default = translate("{zone} (platform default)", locale, zone=platform_default)
    return [("", default)] + [(zone, zone) for zone in zones]
