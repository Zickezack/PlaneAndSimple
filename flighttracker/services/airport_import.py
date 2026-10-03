"""Import countries and airports from OurAirports (public domain, https://ourairports.com/data/).

Passenger numbers for ranking airports by size come from Wikidata (CC0, property P3872).
They are optional: if Wikidata is unreachable, the import still succeeds and previously
imported numbers are kept.
"""

import csv
import io
import logging
import re
from collections.abc import Iterable

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from flighttracker.models import Airport, Country

log = logging.getLogger(__name__)

COUNTRIES_URL = "https://davidmegginson.github.io/ourairports-data/countries.csv"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
# Highest yearly passenger count since 2019 per IATA code.
PASSENGERS_QUERY = """
SELECT ?iata (MAX(?pax) AS ?passengers) WHERE {
  ?airport wdt:P238 ?iata ; p:P3872 ?statement .
  ?statement ps:P3872 ?pax ; pq:P585 ?when .
  FILTER(YEAR(?when) >= 2019)
} GROUP BY ?iata
"""

_IATA = re.compile(r"[A-Z]{3}")
_TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
_CHUNK = 1000


def parse_countries(csv_text: str) -> list[dict]:
    return [
        {"code": row["code"], "name": row["name"][:100], "continent": row["continent"] or None}
        for row in csv.DictReader(io.StringIO(csv_text))
        if len(row["code"]) == 2
    ]


def parse_airports(csv_text: str, known_countries: set[str]) -> list[dict]:
    """Airports with a valid IATA code; duplicates resolved in favour of the busiest type."""
    best: dict[str, tuple[tuple[int, int], dict]] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        iata = row["iata_code"].strip().upper()
        if not _IATA.fullmatch(iata) or row["type"] == "closed":
            continue
        if row["iso_country"] not in known_countries:
            continue
        scheduled = row["scheduled_service"] == "yes"
        rank = (0 if scheduled else 1, _TYPE_RANK.get(row["type"], 9))
        record = {
            "iata_code": iata,
            "name": row["name"][:200],
            "city": row["municipality"][:100] or None,
            "country_code": row["iso_country"],
            "airport_type": row["type"][:30],
            "has_scheduled_service": scheduled,
        }
        if iata not in best or rank < best[iata][0]:
            best[iata] = (rank, record)
    return [record for _, record in best.values()]


def parse_passengers(payload: dict) -> dict[str, int]:
    result = {}
    for binding in payload.get("results", {}).get("bindings", []):
        try:
            iata = binding["iata"]["value"].strip().upper()
            passengers = int(float(binding["passengers"]["value"]))
        except (KeyError, TypeError, ValueError):
            continue
        if _IATA.fullmatch(iata) and passengers > 0:
            result[iata] = passengers
    return result


def wikidata_user_agent(contact: str) -> str:
    # Wikimedia's robot policy requires contact information; requests without it get HTTP 403.
    return f"PlaneAndSimple/0.1 (self-hosted flight price tracker; {contact})"


def fetch_passengers(
    client: httpx.Client, contact: str, url: str = WIKIDATA_SPARQL_URL
) -> dict[str, int]:
    """Passenger numbers by IATA code; empty if Wikidata is unavailable or no contact is set."""
    if not contact:
        log.warning("WIKIDATA_CONTACT is not set – airports are only ranked by type.")
        return {}
    try:
        response = client.post(
            url,
            data={"query": PASSENGERS_QUERY},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": wikidata_user_agent(contact),
            },
        ).raise_for_status()
        return parse_passengers(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Passenger numbers from Wikidata unavailable (%s)", type(exc).__name__)
        return {}


def _chunks(rows: list[dict]) -> Iterable[list[dict]]:
    for start in range(0, len(rows), _CHUNK):
        yield rows[start : start + _CHUNK]


def upsert_countries(session: Session, rows: list[dict]) -> None:
    for chunk in _chunks(rows):
        statement = insert(Country).values(chunk)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Country.code],
                set_={"name": statement.excluded.name, "continent": statement.excluded.continent},
            )
        )


_AIRPORT_COLUMNS = ("name", "city", "country_code", "airport_type", "has_scheduled_service")


def upsert_airports(session: Session, rows: list[dict], passengers: dict[str, int]) -> None:
    """Without passenger data (Wikidata down) the stored numbers are left untouched."""
    columns = _AIRPORT_COLUMNS + (("passengers",) if passengers else ())
    rows = [row | {"passengers": passengers.get(row["iata_code"])} for row in rows]
    for chunk in _chunks(rows):
        statement = insert(Airport).values(chunk)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Airport.iata_code],
                set_={column: statement.excluded[column] for column in columns},
            )
        )


def import_needed(session: Session, *, wikidata_contact: str = "") -> bool:
    """True until airports – and, with a Wikidata contact, passenger numbers – are imported.

    Without a contact no passenger numbers can ever arrive, so the download is not repeated
    on every start just because they are missing.
    """
    if session.scalar(select(func.count()).select_from(Airport)) == 0:
        return True
    ranked = session.scalar(select(func.count()).where(Airport.passengers.is_not(None)))
    return bool(wikidata_contact) and ranked == 0


def import_from_ourairports(
    session: Session,
    *,
    wikidata_contact: str = "",
    countries_url: str = COUNTRIES_URL,
    airports_url: str = AIRPORTS_URL,
    passengers_url: str = WIKIDATA_SPARQL_URL,
) -> tuple[int, int, int]:
    """Returns the number of countries, airports and airports with passenger numbers."""
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        countries_csv = client.get(countries_url).raise_for_status().text
        airports_csv = client.get(airports_url).raise_for_status().text
        passengers = fetch_passengers(client, wikidata_contact, passengers_url)
    countries = parse_countries(countries_csv)
    airports = parse_airports(airports_csv, {c["code"] for c in countries})
    upsert_countries(session, countries)
    upsert_airports(session, airports, passengers)
    ranked = sum(1 for airport in airports if airport["iata_code"] in passengers)
    return len(countries), len(airports), ranked
