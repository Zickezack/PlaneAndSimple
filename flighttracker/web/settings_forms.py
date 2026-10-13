"""Form helpers of the Settings page: Platform Settings sections and their fields, option
lists (currencies, time zones), the SMTP/AI presets and the setup status at the top."""

from dataclasses import dataclass
from zoneinfo import available_timezones

from flighttracker.config import Settings
from flighttracker.domain.ai_providers import PRESETS_BY_KEY as AI_PRESETS_BY_KEY
from flighttracker.domain.currencies import CURRENCIES
from flighttracker.domain.mail_providers import PROVIDERS_BY_KEY
from flighttracker.i18n import currency_name, translate
from flighttracker.providers.registry import PROVIDERS, provider_class
from flighttracker.services.ai import ai_enabled
from flighttracker.services.mail import mail_enabled
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
        "value": "" if is_secret or value is None else value,
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


@dataclass(frozen=True)
class SetupItem:
    """One platform service at the top of Platform Settings: set up or not, how important it
    is ("needed", "recommended", "optional") and what is missing without it."""

    label: str
    ok: bool
    level: str
    missing: str  # consequence when not set up
    anchor: str


def setup_status(settings: Settings) -> list[SetupItem]:
    provider = settings.flight_provider
    token_ok = provider != "travelpayouts" or bool(settings.travelpayouts_token)
    mail_on = mail_enabled(settings)
    items = [
        SetupItem(
            "Flight data",
            not provider_class(provider).fake_data and token_ok,
            "needed",
            "Invented demo prices (mock) or a provider without its token – no real fares.",
            "providers",
        ),
        SetupItem(
            "Mail server",
            mail_on,
            "recommended",
            'No welcome mails, no "Forgot password?", no price alerts.',
            "mail",
        ),
    ]
    if mail_on:
        items.append(
            SetupItem(
                "Public address",
                bool(settings.public_base_url),
                "recommended",
                "Price alert mails from the worker carry no link to the prices.",
                "mail",
            )
        )
    return [
        *items,
        SetupItem(
            "AI",
            ai_enabled(settings),
            "optional",
            "Optional: no drafts from a description and no AI explanations; the rule-based "
            "advice works without.",
            "ai",
        ),
        SetupItem(
            "Wikidata contact",
            bool(settings.wikidata_contact),
            "optional",
            "Airports of a country are ranked by type only, not by passenger numbers.",
            "airport_import",
        ),
    ]
