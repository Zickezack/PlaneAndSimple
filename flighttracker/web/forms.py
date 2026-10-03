"""Parses the Suchabo HTML form into a validated `SearchInput`.

Error messages are `Msg` objects (English source texts), translated when rendered.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError
from starlette.datastructures import FormData

from flighttracker.config import Settings
from flighttracker.domain.filters import CabinClass, SearchFilters, TripType
from flighttracker.domain.locations import (
    country_codes,
    format_location_codes,
    parse_location_codes,
)
from flighttracker.domain.spec import SearchSpec
from flighttracker.i18n import Msg
from flighttracker.models import Search
from flighttracker.services.searches import SearchInput, spec_of

MAX_NAME_LENGTH = 120

_FIELD_LABELS = {
    "cabin_classes": "Cabin class",
    "max_stops": "Stops",
    "months_ahead": "Period",
    "days_per_month": "Departure dates sampled per month",
    "stay_days_min": "Min. stay",
    "stay_days_max": "Max. stay",
    "adults": "Adults",
    "children": "Children",
    "currency": "Currency",
}


@dataclass
class SearchFormResult:
    values: dict
    errors: list[Msg] = field(default_factory=list)
    data: SearchInput | None = None
    # Countries entered without their airport checkboxes having been shown yet.
    unreviewed_countries: set[str] = field(default_factory=set)


def airport_field_name(country: str) -> str:
    return f"airports_{country}"


def default_values(settings: Settings) -> dict:
    return {
        "name": "",
        "origins": "",
        "destinations": "",
        "trip_type": TripType.ROUND_TRIP.value,
        "cabin_classes": [CabinClass.ECONOMY.value],
        "max_stops": "any",
        "months_ahead": "6",
        "days_per_month": str(settings.scraper_days_per_month),
        "stay_days_min": "",
        "stay_days_max": "",
        "adults": "1",
        "children": "0",
        "currency": settings.default_currency,
        "poll_interval_hours": str(max(settings.default_poll_interval_minutes // 60, 1)),
        "airport_countries": [],
        "country_airports": {},
    }


def values_from_spec(name: str, spec: SearchSpec, poll_interval_minutes: int) -> dict:
    f = spec.filters
    return {
        "name": name,
        "origins": format_location_codes(spec.origins),
        "destinations": format_location_codes(spec.destinations),
        "trip_type": f.trip_type.value,
        "cabin_classes": sorted(c.value for c in f.cabin_classes),
        "max_stops": "any" if f.max_stops is None else str(f.max_stops),
        "months_ahead": str(f.months_ahead),
        "days_per_month": str(f.days_per_month),
        "stay_days_min": "" if f.stay_days_min is None else str(f.stay_days_min),
        "stay_days_max": "" if f.stay_days_max is None else str(f.stay_days_max),
        "adults": str(f.adults),
        "children": str(f.children),
        "currency": f.currency,
        "poll_interval_hours": str(max(poll_interval_minutes // 60, 1)),
        **airport_values(spec),
    }


def airport_values(spec: SearchSpec) -> dict:
    """Form values for the airport checkboxes of every country in the spec."""
    return {
        "airport_countries": sorted(spec.country_codes()),
        "country_airports": {
            code: sorted(airports) for code, airports in spec.country_airports.items()
        },
    }


def values_from_search(search: Search) -> dict:
    return values_from_spec(search.name, spec_of(search), search.poll_interval_minutes)


def _text(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _optional_int(raw: str, label: str, errors: list[Msg]) -> int | None:
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        errors.append(Msg("{label}: please enter a whole number.", label=Msg(label)))
        return None


def _pydantic_messages(exc: ValidationError) -> list[Msg]:
    """Our own validator texts are translated; pydantic's built-in ones stay English."""
    messages = []
    for error in exc.errors():
        message = Msg(error["msg"].removeprefix("Value error, "))
        location = error["loc"][0] if error["loc"] else None
        label = _FIELD_LABELS.get(str(location))
        messages.append(
            Msg("{label}: {message}", label=Msg(label), message=message) if label else message
        )
    return messages


def parse_search_form(
    form: FormData,
    *,
    default_currency: str = "CHF",
    poll_interval_minutes: int = 360,
) -> SearchFormResult:
    values = {
        key: _text(form, key)
        for key in (
            "name",
            "origins",
            "destinations",
            "trip_type",
            "max_stops",
            "months_ahead",
            "days_per_month",
            "stay_days_min",
            "stay_days_max",
            "adults",
            "children",
        )
    }
    values["currency"] = default_currency.upper()
    values["poll_interval_hours"] = str(max(poll_interval_minutes // 60, 1))
    values["cabin_classes"] = [v for v in form.getlist("cabin_classes") if isinstance(v, str)]
    # Countries whose checkboxes were part of the submitted form (hidden field).
    values["airport_countries"] = sorted(
        code for code in _text(form, "airport_countries").upper().split(",") if code
    )
    values["country_airports"] = {
        code: sorted(
            {v.upper() for v in form.getlist(airport_field_name(code)) if isinstance(v, str)}
        )
        for code in values["airport_countries"]
    }
    result = SearchFormResult(values=values)
    errors = result.errors

    name = values["name"]
    if not name:
        errors.append(Msg("Please enter a name."))
    elif len(name) > MAX_NAME_LENGTH:
        errors.append(Msg("The name may be at most {n} characters long.", n=MAX_NAME_LENGTH))

    origins, origin_errors = parse_location_codes(values["origins"])
    destinations, destination_errors = parse_location_codes(values["destinations"])
    errors.extend(Msg("Origin: {error}", error=e) for e in origin_errors)
    errors.extend(Msg("Destination: {error}", error=e) for e in destination_errors)

    countries = country_codes(origins | destinations)
    reviewed = countries & set(values["airport_countries"])
    result.unreviewed_countries = countries - reviewed
    for code in sorted(reviewed):
        if not values["country_airports"][code]:
            errors.append(Msg('Please select at least one airport for "{code}".', code=code))

    stops_raw = values["max_stops"]
    filters_input = {
        "trip_type": values["trip_type"],
        "cabin_classes": values["cabin_classes"],
        "max_stops": None
        if stops_raw in ("", "any")
        else _optional_int(stops_raw, "Stops", errors),
        "months_ahead": _optional_int(values["months_ahead"], "Period", errors),
        "days_per_month": _optional_int(
            values["days_per_month"], "Departure dates sampled per month", errors
        ),
        "stay_days_min": _optional_int(values["stay_days_min"], "Min. stay", errors),
        "stay_days_max": _optional_int(values["stay_days_max"], "Max. stay", errors),
        "adults": _optional_int(values["adults"], "Adults", errors),
        "children": _optional_int(values["children"], "Children", errors),
        "currency": values["currency"],
    }
    if not filters_input["cabin_classes"]:
        errors.append(Msg("Please select at least one cabin class."))
    if errors:
        return result
    try:
        # Empty fields fall back to the model defaults (None = no limit).
        filters = SearchFilters.model_validate(
            {k: v for k, v in filters_input.items() if v is not None}
        )
    except ValidationError as exc:
        errors.extend(_pydantic_messages(exc))
        return result

    country_airports = {code: frozenset(values["country_airports"][code]) for code in reviewed}
    result.data = SearchInput(
        name=name,
        spec=SearchSpec(origins, destinations, filters, country_airports),
        poll_interval_minutes=poll_interval_minutes,
    )
    return result
