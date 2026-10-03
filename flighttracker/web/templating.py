from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from flighttracker.i18n import SUPPORTED_LOCALES, Msg, translate
from flighttracker.providers.registry import provider_class
from flighttracker.services.settings import effective_settings
from flighttracker.web import flights, labels
from flighttracker.web.deps import current_locale, current_user, ensure_csrf_token, pop_flashes

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _static_url(path: str) -> str:
    """URL with the file's modification time, so browsers never keep a stale CSS/JS file."""
    version = int((STATIC_DIR / path).stat().st_mtime)
    return f"/static/{path}?v={version}"


def _request_context(request: Request) -> dict:
    locale = current_locale(request)

    def gettext(text: str | Msg, **params) -> str:
        """`{{ _("Text {n}", n=1) }}` in templates; also renders `Msg` objects."""
        return text.render(locale) if isinstance(text, Msg) else translate(text, locale, **params)

    base_settings = request.app.state.settings
    user = current_user(request)
    # Platform Settings may override the active provider; cheap enough to check per request.
    with request.app.state.session_factory() as session:
        flight_provider = effective_settings(session, base_settings).flight_provider

    return {
        "csrf_token": ensure_csrf_token(request),
        "current_user": user,
        "flashes": pop_flashes(request),
        "locale": locale,
        "locales": SUPPORTED_LOCALES,
        "_": gettext,
        "provider": provider_class(flight_provider),
        # Personal time zone (validated when saved), else the platform's.
        "timezone": ZoneInfo((user and user.timezone) or base_settings.display_timezone),
    }


def _format_number(value, locale: str, decimals: int = 0) -> str:
    formatted = f"{value:,.{decimals}f}"
    # German UI uses Swiss formatting (1'234.50), English 1,234.50.
    return formatted.replace(",", "'") if locale == "de" else formatted


@pass_context
def _format_price(context, value, currency: str = "") -> str:
    if value is None:
        return "–"
    return f"{_format_number(value, context['locale'], 2)} {currency}".strip()


@pass_context
def _format_datetime(context, value) -> str:
    """Stored timestamps are UTC; shown in the configured zone (Europe/Zurich by default)."""
    if not value:
        return "–"
    local = value.astimezone(context["timezone"])
    return local.strftime("%d.%m.%Y %H:%M" if context["locale"] == "de" else "%Y-%m-%d %H:%M")


@pass_context
def _format_date(context, value) -> str:
    if not value:
        return "–"
    return value.strftime("%d.%m.%Y" if context["locale"] == "de" else "%Y-%m-%d")


_WEEKDAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
}
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@pass_context
def _format_weekday_date(context, value) -> str:
    """Departure dates with weekday – the weekday matters when choosing a flight."""
    if not value:
        return "–"
    locale = context["locale"]
    weekday = _WEEKDAYS.get(locale, _WEEKDAYS["en"])[value.weekday()]
    if locale == "de":
        return f"{weekday}, {value.strftime('%d.%m.%Y')}"
    return f"{weekday}, {value.day} {_MONTHS_EN[value.month - 1]} {value.year}"


@pass_context
def _format_iso_weekday_date(context, value: str | None) -> str:
    return _format_weekday_date(context, date.fromisoformat(value)) if value else "–"


@pass_context
def _stay_option(context, option: tuple[str, str]) -> tuple[str, str]:
    value, _ = option
    return value, translate("{n} days", context["locale"], n=value)


@pass_context
def _format_month(context, value) -> str:
    if not value:
        return "–"
    return value.strftime("%m.%Y" if context["locale"] == "de" else "%Y-%m")


@pass_context
def _airport_label(context, option) -> tuple[str, str]:
    """(value, text) pair for the airport checkboxes."""
    locale = context["locale"]
    if option.passengers is None:
        passengers = translate("passengers unknown", locale)
    elif option.passengers >= 1_000_000:
        millions = _format_number(option.passengers / 1_000_000, locale, 1)
        passengers = translate("{n}M passengers", locale, n=millions)
    else:
        passengers = translate(
            "{n} passengers", locale, n=_format_number(option.passengers, locale)
        )
    city = f", {option.city}" if option.city else ""
    return option.iata_code, f"{option.iata_code} – {option.name}{city} · {passengers}"


@pass_context
def _translate_options(context, options) -> list[tuple[str, str]]:
    """Translate the texts of (value, text) pairs, e.g. `labels.STOPS_OPTIONS | tr_options`."""
    return [(value, context["_"](text)) for value, text in options]


templates = Jinja2Templates(directory=TEMPLATE_DIR, context_processors=[_request_context])
templates.env.globals["labels"] = labels
templates.env.globals["static_url"] = _static_url
templates.env.globals["sparkline"] = flights.sparkline
templates.env.filters["price"] = _format_price
templates.env.filters["datetime"] = _format_datetime
templates.env.filters["date"] = _format_date
templates.env.filters["month"] = _format_month
templates.env.filters["airport_label"] = _airport_label
templates.env.filters["tr_options"] = _translate_options
templates.env.filters["weekday_date"] = _format_weekday_date
templates.env.filters["stay_option"] = _stay_option
templates.env.filters["iso_weekday_date"] = _format_iso_weekday_date
