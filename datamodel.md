# Data Model

This document describes the project's key entities, relationships, and data constraints. Update it as the domain becomes clearer.

## Purpose
- Capture the core domain model for the application.
- Define entities, attributes, and relationships.
- Identify validation, privacy, and persistence requirements.

## Glossary (UI English / German ↔ code)

The code and docs use "Suchabo" for a tracked search.

| UI (English) | UI (Deutsch) | Code / table | Meaning |
|---|---|---|---|
| Tracked search | Suchabo | `Search` / `searches` | Origins, destinations and filters; cadence is global |
| Origin / Destination | Abflug / Ziel | `SearchLocation` (role `origin` / `destination`) | Airport (IATA, 3 letters) or whole country (ISO, 2 letters) |
| Trip type | Reiseart | `trip_type` | `round_trip` / `one_way` |
| Cabin class | Kabinenklasse | `cabin_classes` / `cabin_class` | `economy`, `premium_economy`, `business`, `first` |
| Stops | Stopps | `max_stops` | `None` = any, `0` = direct flights only |
| Period | Zeitraum | `months_ahead` | Departure months tracked, starting with the current month |
| Stay | Aufenthalt | `stay_days_min` / `stay_days_max` | Length of stay (round trips only) |
| Departure dates sampled per month | Abflugtermine pro Monat | `days_per_month` | Sampling count for some providers (4/8/15/31, default 4), separate from poll cadence |
| Adults / Children | Erwachsene / Kinder | `adults` / `children` | Passengers; children aged 2–11, at most 9 passengers in total |
| Poll interval | Abfrage-Intervall | `default_poll_interval_minutes` | One platform-wide cadence for active Suchabos and Trips, in minutes (default 1440) |
| Price history | Preis-Historie | `PriceHistory` / `price_history` | Append-only observed prices |
| Revision / Change history | Revision / Änderungsverlauf | `SearchRevision` / `search_revisions` | Immutable snapshot after each change |
| Airports per country | Flughäfen je Land | `search_locations.airport_codes` | Airports of a country location that are queried |
| Poll (job) | Abfrage (Job) | `FetchJob` / `fetch_jobs` | `poll` (regular query; the former `backfill` kind was removed) |
| Archive | Archivieren | `searches.archived_at` | Soft delete; data kept |
| Delete permanently | Endgültig löschen | `DELETE` cascade | Removes the Suchabo including all history |
| Log | Protokoll | `QueryLog` / `query_log` | One entry per provider query: `ok` (prices found), `empty` (no flights), `failed` |
| Invented | Erfunden | `price_history.provider` in `FAKE_DATA_PROVIDERS` | Prices from a fake-data provider (`mock`), always marked in red |
| – | – | `price_history.source` | New rows are always `live`; `backfill` only in rows from before migration `0002` |
| Trip | Trip | `Trip` / `trips` | Bounded planner window; uses the global polling cadence |
| Trip leg | Teilstrecke | `TripLeg` / `trip_legs` | Ordered link to an ordinary one-way Suchabo; calendar-day stay bounds apply before this leg |
| User | Benutzer | `User` / `users` | Login with role `admin` or `user`, personal settings and quota overrides |
| Owner | Besitzer | `searches.owner_id`, `trips.owner_id` | User a Suchabo/Trip belongs to; `NULL` only until the env admin's next login |
| Sharing | Teilen | `Share` / `shares` | Access of one user to one Suchabo or Trip: view only (`can_edit` false) or view and edit |
| Cancelled | Abgebrochen | `fetch_jobs.status = 'cancelled'` | Poll stopped from the Poll Log |

## Diagram

```mermaid
erDiagram
    countries ||--o{ airports : has
    searches ||--|{ search_locations : "origins/destinations"
    trips ||--|{ trip_legs : "ordered itinerary"
    searches ||--o| trip_legs : "one ordinary Suchabo per leg"
    airports |o--o{ search_locations : "airport_code"
    countries |o--o{ search_locations : "country_code"
    searches ||--|{ search_revisions : "versions"
    searches ||--o{ price_history : "observations"
    search_revisions ||--o{ price_history : "recorded under"
    searches ||--o{ fetch_jobs : "queue"
    trips |o--o{ fetch_jobs : "coordinated poll"
    searches |o..o{ query_log : "search_id (no FK)"
    users |o--o{ searches : owns
    users |o--o{ trips : owns
    users ||--o{ shares : "has access"
    searches |o--o{ shares : "shared"
    trips |o--o{ shares : "shared"

    countries { char2 code PK; string name; char2 continent }
    airports { char3 iata_code PK; string name; string city; char2 country_code FK; string airport_type; bool has_scheduled_service; bigint passengers }
    searches { bigint id PK; bigint owner_id FK; string name; enum status; jsonb filters; int poll_interval_minutes; int revision_no; timestamptz next_poll_at; timestamptz last_polled_at; timestamptz archived_at }
    search_locations { bigint id PK; bigint search_id FK; enum role; char3 airport_code FK; char2 country_code FK; char3_array airport_codes }
    search_revisions { bigint id PK; bigint search_id FK; int revision_no; jsonb snapshot }
    price_history { bigint id PK; bigint search_id FK; bigint search_revision_id FK; enum source; string provider; char3 origin_iata; char3 destination_iata; date departure_date; date return_date; enum cabin_class; smallint stops; numeric price; char3 currency; smallint adults; smallint children; string airline; jsonb details; timestamptz observed_at; timestamptz fetched_at }
    fetch_jobs { bigint id PK; bigint search_id FK; bigint trip_id FK; enum kind; enum status; timestamptz run_after; int attempts; int queries_total; int queries_failed; int quotes_stored; text last_error; timestamptz created_at; timestamptz started_at; timestamptz finished_at }
    trips { bigint id PK; bigint owner_id FK; string name; date starts_on; date ends_on; int poll_interval_minutes; enum status; timestamptz next_poll_at; timestamptz last_polled_at; timestamptz archived_at }
    trip_legs { bigint id PK; bigint trip_id FK; bigint search_id FK; int position; int min_layover_days; int max_layover_days }
    query_log { bigint id PK; bigint job_id; bigint search_id; string search_name; string provider; char3 origin_iata; char3 destination_iata; date departure_month; enum cabin_class; enum outcome; int quotes_found; int quotes_stored; text error; jsonb results; timestamptz started_at; int duration_ms; timestamptz logged_at }
    platform_settings { string key PK; text value; timestamptz updated_at }
    users { bigint id PK; string username; text password_hash; enum role; bool is_active; int max_searches; int max_requests; string locale; string timezone; int auth_version; timestamptz created_at; timestamptz last_login_at }
    shares { bigint id PK; bigint search_id FK; bigint trip_id FK; bigint user_id FK; bool can_edit; timestamptz created_at }
```

## Constraints and rules

- `searches.filters` is JSONB validated by `SearchFilters` (`flighttracker/domain/filters.py`,
  `extra="forbid"`). New filters need no migration, but must get a default value so that
  existing rows stay valid (e.g. `children`, default 0).
- `searches.poll_interval_minutes` and `trips.poll_interval_minutes` remain legacy metadata for
  imported records. Scheduling and new provider queries use global platform settings instead.
  Global currency selects future provider quotes; stored price rows retain their observed currency
  and are never converted retroactively.
- `search_locations`: exactly one of `airport_code` / `country_code` is set (CHECK); unique per
  Suchabo and role (`NULLS NOT DISTINCT`). Country rows carry the selected airports in
  `airport_codes` (CHECK: set exactly for country rows); origin and destination rows of the same
  country share one selection. The selection is part of every revision snapshot
  (`country_airports`).
- `query_log` is append-only too and has **no foreign keys** on purpose: it is a global,
  persistent log that outlives jobs and permanently deleted Suchabos (name kept as snapshot).
  `results` repeats the prices a query found (see below); it contains no personal data.
- `price_history` and `search_revisions` are **append-only** (trigger `forbid_update()`).
  Every price row carries its own route, dates, cabin and stops, so it stays valid when the
  Suchabo changes. Duplicates are ignored via `uq_price_history_observation`.
- `platform_settings` holds the admin's overrides of selected `.env` values (Settings →
  Platform Settings), one row per key. Only keys in `services/settings.py` `SETTING_FIELDS` are
  written; a value that no longer validates is ignored, and deleting a row falls back to `.env`.
- All foreign keys to `searches` use `ON DELETE CASCADE`. They only take effect on permanent
  deletion. The normal "delete" in the UI is archiving (`archived_at`).
- A Trip consists of 2–8 ordinary one-way Suchabos, ordered by `trip_legs.position`. Each leg can
  expand a country endpoint into its selected airports; the worker queries the resulting concrete
  routes and only joins quotes at the same IATA airport. The first
  leg's departure and the final leg's arrival must fall inside the inclusive `starts_on`–`ends_on`
  window (maximum 60 days). A Trip poll queries the first leg's dates in that window; later legs
  are queried only on calendar dates that could satisfy the preceding flight's arrival plus the
  configured calendar-day bounds. Day bounds count the difference between local arrival and
  departure dates; exact local flight times are still required internally to reject backwards or
  impossible connections. Trip legs are excluded from normal monthly polling. Times are usable
  only when the active provider returns local departure and arrival times; the planner currently
  requires that capability (Google Flights and the deterministic mock provider provide it).
- A displayed complete-option price is the sum of the individual one-way prices. It is not a
  through-ticket fare, and connections may involve separately booked tickets and self-transfers.
- `users.username` is unique ignoring case (`uq_users_username_lower`). `password_hash` is
  `NULL` only for the admin from `.env`, whose password lives in `ADMIN_PASSWORD_HASH`.
  `max_searches` / `max_requests` `NULL` = the Platform Settings defaults; `locale` /
  `timezone` `NULL` = the platform defaults. `auth_version` is raised on password, role and
  status changes and ends older sessions.
- `searches.owner_id` / `trips.owner_id`: rows from before migration `0009` start with `NULL`
  and are assigned to the env admin on its login. Deleting a user moves their Suchabos and
  Trips to the env admin (`ON DELETE SET NULL` is only the safety net); existing shares of them
  stay. Leg Suchabos have their Trip's owner.
- `shares`: exactly one of `search_id` / `trip_id` (CHECK), one row per target and user
  (`NULLS NOT DISTINCT`), cascades with the Suchabo, Trip or user. Trip legs are never shared
  individually.
- `price_history.adults` / `children`: passengers the price was requested for; prices are only
  compared within the same passengers. Rows from before migration `0010` took them from their
  revision's snapshot.
- Enums are `VARCHAR` + `CHECK` (not native PG enums), so adding values is a simple migration.
- `countries` / `airports` come from OurAirports (public domain) via
  `python -m flighttracker.cli import-airports` (run automatically by `migrate` until it succeeded
  once – with `WIKIDATA_CONTACT` set, until passenger numbers arrived too). `airports.passengers` is the highest yearly passenger count since 2019 from Wikidata
  (CC0) and only used for ranking; it needs `WIKIDATA_CONTACT`. Selectable airports for a country
  have scheduled service and type `large_airport` / `medium_airport`.

## Flight details (`price_history.details`)

JSONB written by providers that know the flight (currently Google Flights; `NULL` otherwise and
for rows before migration `0004`). It describes the **outbound** itinerary as shown by the
source; for round trips the price covers the whole trip, the return flight is chosen on the
source's site. All fields are optional:

```json
{
  "airlines": ["Qatar Airways"],
  "departure_time": "15:10", "arrival_date": "2026-11-14", "arrival_time": "18:50",
  "duration_min": 2740,
  "segments": [{"flight": "QR 96", "from": "ZRH", "to": "DOH",
                "departure_date": "2026-11-12", "departure_time": "15:10",
                "arrival_date": "2026-11-12", "arrival_time": "22:50",
                "duration_min": 340, "aircraft": "Airbus A350"}],
  "url": "https://www.google.com/travel/flights?tfs=…"
}
```

**Price point** (read model, `services/history.py`): the current price for one route, cabin,
departure and return date = the latest observation (whatever the stops); `previous_price` is the
same flight dates at the poll before. Several stay lengths give several points per day. The chart
shows the cheapest point per date (optionally for one stay length); the table lists all points.

`query_log.results` lists what a query found: `[{"date", "return", "price", "currency", "stops"}]`.

## Privacy

**Personal data:** `users` (username, scrypt password hash, last login, language and time
zone) and indirectly `shares`, `searches.owner_id` and `trips.owner_id` (what a user tracks and
with whom they share it). Deleting a user removes the row and their shares; their Suchabos and
Trips stay without owner. Usernames are shown to other users only where they share something
(owner, share list). The JSON export contains no user data.

The env admin's password hash lives in environment variables. The only other credential that can
end up in the database is a Travelpayouts token entered under Platform Settings
(`platform_settings`, plain text).
