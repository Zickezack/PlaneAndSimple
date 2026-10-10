"""Trip Planner form: default values, parsing the posted legs, the input for create_trip."""

from datetime import date, timedelta

from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from flighttracker.domain.filters import CabinClass
from flighttracker.services.locations import airport_options
from flighttracker.services.trips import (
    TripInput,
    TripLegInput,
)


def default_trip_values(settings=None) -> dict:
    starts = date.today() + timedelta(days=14)
    interval = settings.default_poll_interval_minutes if settings else 1440
    currency = settings.default_currency if settings else "CHF"
    return {
        "name": "",
        "starts_on": starts.isoformat(),
        "ends_on": (starts + timedelta(days=7)).isoformat(),
        "poll_interval_hours": str(max(interval // 60, 1)),
        "adults": "1",
        "children": "0",
        "currency": currency,
        "cabin_classes": [CabinClass.ECONOMY.value],
        "max_stops": "any",
        "legs": [
            {
                "origin": "",
                "destination": "",
                "min_layover_days": "",
                "max_layover_days": "",
                "airport_countries": [],
                "country_airports": {},
            },
            {
                "origin": "",
                "destination": "",
                "min_layover_days": "1",
                "max_layover_days": "",
                "airport_countries": [],
                "country_airports": {},
            },
        ],
    }


def trip_airport_field_name(index: int, country: str) -> str:
    return f"trip_airports_{index}_{country}"


def trip_values_from_form(form: FormData, settings) -> dict:
    values = default_trip_values(settings)
    values.update(
        {
            "name": str(form.get("name", "")).strip(),
            "starts_on": str(form.get("starts_on", "")),
            "ends_on": str(form.get("ends_on", "")),
            "adults": str(form.get("adults", "1")),
            "children": str(form.get("children", "0")),
            "cabin_classes": form.getlist("cabin_classes"),
            "max_stops": str(form.get("max_stops", "any")),
        }
    )
    origins = form.getlist("leg_origin")
    destinations = form.getlist("leg_destination")
    min_layovers = form.getlist("min_layover_days")
    max_layovers = form.getlist("max_layover_days")
    reviewed = set(form.getlist("trip_airports_reviewed"))
    values["legs"] = []
    if len(origins) != len(destinations):
        raise ValueError("Every leg needs both an origin and a destination airport.")
    for index, (origin, destination) in enumerate(zip(origins, destinations, strict=True)):
        minimum = str(min_layovers[index]).strip() if index < len(min_layovers) else ""
        maximum = str(max_layovers[index]).strip() if index < len(max_layovers) else ""
        airport_countries = sorted(
            {code for code in (str(origin).upper(), str(destination).upper()) if len(code) == 2}
        )
        airport_selections = {
            code: sorted(
                {
                    value.upper()
                    for value in form.getlist(trip_airport_field_name(index, code))
                    if isinstance(value, str)
                }
            )
            for code in airport_countries
        }
        leg_values = {
            "origin": str(origin).strip().upper(),
            "destination": str(destination).strip().upper(),
            "min_layover_days": minimum,
            "max_layover_days": maximum,
            "airport_countries": airport_countries,
            "country_airports": airport_selections,
            "reviewed_airports": {
                code for code in airport_countries if f"{index}:{code}" in reviewed
            },
        }
        values["legs"].append(leg_values)
    return values


def trip_input_from_values(values: dict, settings) -> TripInput:
    legs = [
        TripLegInput(
            origin=leg["origin"],
            destination=leg["destination"],
            min_layover_days=int(leg["min_layover_days"]) if leg["min_layover_days"] else None,
            max_layover_days=int(leg["max_layover_days"]) if leg["max_layover_days"] else None,
            country_airports={
                code: frozenset(airports) for code, airports in leg["country_airports"].items()
            },
        )
        for leg in values["legs"]
    ]
    start = date.fromisoformat(values["starts_on"])
    end = date.fromisoformat(values["ends_on"])
    max_stops = None if values["max_stops"] == "any" else int(values["max_stops"])
    return TripInput(
        name=values["name"],
        starts_on=start,
        ends_on=end,
        poll_interval_minutes=settings.default_poll_interval_minutes,
        legs=tuple(legs),
        cabin_classes=frozenset(CabinClass(value) for value in values["cabin_classes"]),
        max_stops=max_stops,
        adults=int(values["adults"]),
        currency=settings.default_currency,
        children=int(values["children"]),
    )


def review_country_airports(db: Session, settings, values: dict) -> bool:
    countries = {code for leg in values["legs"] for code in leg["airport_countries"]}
    options = airport_options(db, countries)
    needs_review = False
    for leg in values["legs"]:
        for code in leg["airport_countries"]:
            if code not in leg["reviewed_airports"]:
                leg["country_airports"][code] = [
                    option.iata_code
                    for option in options.get(code, [])[: settings.country_default_airports]
                ]
                needs_review = True
    return needs_review
