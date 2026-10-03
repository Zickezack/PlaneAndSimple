# Architecture

This document describes the planned structure of the system and the high-level components.

## Main purpose

"Plane and simple" is a self-hosted flight price tracker. Users define *Suchabos* (search
subscriptions: origins, destinations, filters). A background worker regularly collects prices
for them and keeps an append-only price history, so current prices can be compared with the
same departure month of the previous year.

## Structure

- Devcontainers are to be used for quick development.
- I need Claude Code in the devcontainer for development. So after rebuilding it shall also be available.
- Deployment without Portainer: the server clones the GitHub repo (a read-only deploy key if it is a private fork) and runs `scripts/deploy.sh` (`git pull` + `docker compose up -d --build`) manually. Only the main branch is deployed to the live server; optionally a dev branch to a staging server.
- Code changes must be visible immediately in the running dev containers (no rebuild).
- Micro Services used where applicable i.e. Docker Compose structure.
- Preferred services (only use if applicable): Jinja for templates, FastAPI for API, PostgreSQL or MariaDB for SQL (or SQLite for lightweight/temporary) with Alembic for migrations, MongoDB for JSON like DB, otherwise use the most commonly used applicable services.
- At the beginning, the system should be a monorepo, but later on, it can be split into multiple repos if necessary.
- Users with two roles (admin, user) live in the database; the admin from the env file always
  works as a fallback (see "Users and permissions").

## Services (Docker Compose)

| Service   | Purpose                                                                  |
|-----------|--------------------------------------------------------------------------|
| `db`      | PostgreSQL 17. Not published outside the stack.                          |
| `migrate` | One-shot `alembic upgrade head` + first airport import; `web` and `worker` start after it. |
| `web`     | FastAPI + Jinja (server-rendered HTML, English UI with German as second language). Binds to `127.0.0.1` by default – put a reverse proxy with TLS in front. |
| `worker`  | Schedules polls and processes fetch jobs (`python -m flighttracker.worker`). |

`web` and `worker` share one Python package and one image. The job queue is the
`fetch_jobs` table (claimed with `SELECT … FOR UPDATE SKIP LOCKED`), so no Redis/broker is needed.

Worker behaviour (single worker process):
- **Poll now:** the detail page's button queues a poll unless one is already queued or running.
  New prices are added (the history is append-only); the latest poll becomes the current price.
- **Immediate start:** creating, broadening or resuming a Suchabo sends `NOTIFY fetch_jobs`;
  the worker `LISTEN`s and starts at once instead of waiting for `WORKER_TICK_SECONDS`.
  Jobs run one after another, so a new Suchabo waits for a poll that is already running.
- **One commit per query:** a poll job consists of provider queries (route × departure month ×
  cabin). Each query stores its prices and a `query_log` entry and is committed on its own.
  A failed query does not fail the job; the job only fails (and is retried) if all queries fail.
- **Restarts:** a stop request is honoured between two queries (the job goes back to the
  queue). At start the worker requeues jobs left `running` by a killed process without counting
  the attempt. A resumed job skips the queries it already completed.
- **Global search settings:** all active Suchabos and Trips use the same platform poll interval
  and provider currency. Historical prices keep their recorded currency; there is no conversion.
- **Poll Log:** `query_log` is global and append-only (menu "Poll Log"), with the Suchabo name as a
  snapshot, so entries survive even a permanent deletion.
- **Cancelling:** the Poll Log lists queued and running jobs. Cancelling sets the job to
  `cancelled`; the worker re-reads the status between two queries (and under a row lock before
  its final status change, so a cancellation is never overwritten) and stops. The query in
  progress finishes and is stored; the next regular poll is not affected.
`docker-compose.yml` is the production stack. `.devcontainer/docker-compose.yml` runs the same
services from the `dev` image stage with the repository bind-mounted; `web` and `worker` run under
`watchfiles` and restart on every change of the code or `.env` (polling, since bind mounts from
Windows/WSL deliver no file events). The `app` service is the VS Code / Claude Code workspace.

## Codebase structure

Where new code goes (dependencies only point downwards):

| Layer | Folder | Rules |
|---|---|---|
| Web | `flighttracker/web/` | Routes, form parsing, templates, CSS, `static/js/app.js` (progressive enhancement only – every form must work without JavaScript). No SQL here – call services. Reuse the macros in `templates/_macros.html` for every UI element. |
| Worker | `flighttracker/worker/` | Loop and job orchestration only. |
| Services | `flighttracker/services/` | Use cases on the DB (create/update Suchabo, ingestion, history queries, jobs). Services flush but **never commit** – the route/worker owns the transaction. |
| Providers | `flighttracker/providers/` | One adapter per flight data source implementing `FlightPriceProvider`. Nothing outside this package may import a concrete provider except `registry.py`. |
| Models | `flighttracker/models/` | SQLAlchemy ORM. Every schema change needs an Alembic migration in `migrations/versions/`. |
| Domain | `flighttracker/domain/` | Pure logic (filters, location parsing, coverage/merge rules). No DB, network or framework imports – test with plain unit tests. |

**Plane and simple (UI principle):** every page shows the essentials first and folds the rest
away. Suchabo detail: summary line, "Poll now", chart and a lean flights table; settings, recent
polls, change history and deletion are `<details>` sections; everything about one flight lives on
its flight detail page (`/searches/<id>/flights/<price id>`: segments with Flightradar24 links,
layovers, price history of these dates, Google Flights link). Filters appear only when they offer
a real choice; rarely changed form fields sit under "More options". New features follow this.

**Static files** are referenced with `static_url()`, which appends the file's modification time,
so browsers pick up CSS/JS changes immediately.

**UI languages:** English (default) and German, switchable per session (`/language/<code>`).
Write every UI text in English: `{{ _("Text {n}", n=…) }}` in templates, `Msg("Text {n}", n=…)`
for messages created in Python (validation errors, flash messages – rendered per request), label
tables in `web/labels.py`. Add the German translation to `flighttracker/locales/de.json` (keyed
by the English text); `tests/unit/test_i18n.py` fails on missing or stale entries. Logs, CLI
output and stored error texts (`fetch_jobs.last_error`) are English only.

**Price chart:** `static/js/price-chart.js` draws the detail page's chart as plain SVG (no
library) from JSON embedded in the page (`web/flights.py` → `<script type="application/json">`,
escaped so it cannot close the tag). One line per route in the validated categorical palette
(colour follows the route); more than 8 routes collapse into one "cheapest of n routes" line;
previous-year values appear as a gray context line when a single line is shown. A click pins a
date (diamond markers + a panel with that day's flights and links). The filter row (origin,
destination, cabin, stay length, table view) scopes chart, panel and table. The table lists every
flight (server-rendered, works without JavaScript); the script groups it into "cheapest per
month" rows that unfold into all departure dates.

**Time zone:** timestamps are stored in UTC and shown in `DISPLAY_TIMEZONE` (Europe/Zurich).
Flight times are local airport times as delivered by the source.

**Invented data:** a provider with `fake_data = True` (currently `mock`) invents prices. The UI
must mark such data in red wherever it appears (banner while it is the active provider,
`ui.fake_tag()` and red rows for stored prices from it – `FAKE_DATA_PROVIDERS`).

Tests: `tests/unit/` (no external services) and `tests/integration/` (real PostgreSQL via
`TEST_DATABASE_URL`, schema built from the migrations, each test rolled back).

## Flight data providers (no lock-in)

Selected with `FLIGHT_PROVIDER`. Adding a source = one new class in `providers/` plus a
registry entry; the database and UI stay untouched. Each class declares its capabilities
(`supports_children`, `prices_all_passengers`, `fake_data`); the UI shows a note where a Suchabo
asks for more than the active source can deliver.

Passengers: Google Flights prices adults and children (2–11) and returns the total for all
passengers. Travelpayouts only returns prices for one adult.

| Provider | Data | Notes |
|---|---|---|
| `google_flights` | Google Flights; `fast-flights` (pinned) builds the query and fetches the page, our `parse_fares` reads it | **Recommended, no account needed.** Live fares, one request per sampled departure date × stay length (dates per month set per Suchabo; every stay length from min. to max., at most 8; a failed request is skipped). Sends a consent cookie, otherwise EU/CH requests only get Google's cookie page. Reads both "best" and "other" flights and skips itineraries without a price (fast-flights' own parser misses the first and crashes on the second). Can break whenever Google changes the page; a Terms-of-Service grey area – keep request rates low. |
| `travelpayouts` | Aviasales Data API `v2/prices/month-matrix` | Free token. **Cached** prices from Aviasales user searches (`found_at`), not live fares. Economy/Business/First only; the stay length is filtered client-side. |
| `mock` | Deterministic fake prices | Default in tests; **invented data, always marked in red in the UI**. |

Amadeus Self-Service was shut down on 17.07.2026 and is not an option.

## Trip Planner

A Trip groups 2–8 ordinary one-way Suchabos. Each leg keeps its own price history and appears
under its parent Trip in the Tracked searches list. Each leg accepts airport or country endpoints;
country selections are reviewed per leg and expanded into concrete airport routes. Every leg
shares cabin, maximum stops, adults and children (up to nine passengers total). The first departure
and final arrival are bounded by the Trip's inclusive date window (maximum 60 days), and it uses the
platform-wide poll interval and currency. Its coordinated worker job queries the first leg inside
the window, then only asks
for dates on following legs that could satisfy the preceding arrival and configured min/max
calendar-day stay. Child Suchabos are excluded from the normal monthly scheduler, preventing duplicate and
out-of-window polling.

The planner only claims a complete option when each leg has usable local departure and arrival
times, the connecting airport matches, the calendar-day stay fits, and the whole route completes by the end
date. Google Flights currently provides these details; providers without times cannot be selected
for Trip creation. The displayed total is the sum of individual one-way fares, **not a through-
ticket price**. Separate flights may be separately ticketed and self-transfers are not protected.

## Users and permissions

- **Roles:** `admin` sees and manages everything (users, Platform Settings, export/import, all
  Suchabos and Trips). `user` sees their own Suchabos and Trips and those shared with them;
  the Settings page shows them only their personal settings (language, time zone, password).
- **Ownership:** every Suchabo and Trip has an `owner_id`; a Trip's leg Suchabos always have the
  Trip's owner and are accessed through the Trip. Records from before user management are
  assigned to the env admin on its login; a deleted user's records go to the env admin too.
- **Deactivated owners:** `services.users.pause_unattended` pauses active Suchabos and Trips
  whose owner is deactivated (or being deleted) and that no active user may edit. It runs after
  every change that can remove the last editor (deactivation, deletion, share removed or
  reduced to view), so shared-for-edit records keep running exactly as long as someone is left
  who may edit them.
- **Sharing:** owner and admins share a Suchabo or Trip with single users, `view` or `edit`.
  Editors may edit, merge into, pause/resume and poll; archiving, deleting and sharing stay
  with owner and admins. All checks live in `services/access.py` (`search_access`,
  `visible_searches`, …); routes call them, a missing right is 404 (not visible) or 403.
  The overlap check on creation only looks at Suchabos the user can see.
- **Quota** (`services/quota.py`): per non-admin owner, non-archived Suchabos + Trips
  (`MAX_SEARCHES_PER_USER`) and provider requests per poll of all of them
  (`MAX_REQUESTS_PER_USER`, the real cost driver); per-user overrides on the user's admin page.
  Checked on create, edit, merge and restore; a change that does not grow the usage is always
  allowed.
- **Env admin:** `ADMIN_USERNAME` gets a `users` row on its first login, without password hash –
  its password is always checked against `ADMIN_PASSWORD_HASH`. It cannot be renamed, demoted,
  deactivated or deleted in the UI; no admin can do that to themselves either.

## Price history: guarantees and limits

- **Append-only:** `price_history` and `search_revisions` reject every `UPDATE` (DB trigger).
  Each price row stores its own route, dates, cabin and stops, so editing a Suchabo can never
  change the meaning of past observations. Edits create a new revision; new prices reference it.
- **Soft delete:** archiving sets `searches.archived_at`, and all data stays. Only "endgültig löschen"
  deletes, cascading to the whole history.
- **No backfill:** no free source offers historical fares over months (Travelpayouts only caches
  the last days), so there is no initial backfill. New or broadened Suchabos are polled right
  away; the previous-year comparison fills up through our own tracking.
- **Passengers:** every price row records the passengers it was requested for (`adults`,
  `children`). Prices for other passenger numbers are not comparable (Google Flights prices
  all passengers together), so the detail page, trends, chart and previous-year comparison only
  use observations for the Suchabo's current passengers and say how many others are hidden. A
  passenger change counts as broadening: the Suchabo is polled again right away.
- **Request volume:** a Suchabo costs `routes × cabin classes × months` provider queries per
  poll; for Google Flights each query is `sampled departure dates per month × stay lengths` requests
  (form and detail page show both counts and an estimated duration). A country only expands to its **selected**
  airports: the `COUNTRY_DEFAULT_AIRPORTS` largest (Wikidata passenger numbers) are preselected
  and shown as checkboxes before saving; routes are capped by `MAX_ROUTE_PAIRS_PER_SEARCH`.

## Security

- No secrets in the repository or image: `.env` is git- and docker-ignored; `alembic.ini`
  contains no URL; migrations contain schema only. The production compose aborts if required
  variables are missing, and the app refuses the `.env.example` placeholder `SECRET_KEY`.
- Logins: users in the database (scrypt hashes, minimum 12 characters) plus the env admin
  (`ADMIN_USERNAME`, scrypt `ADMIN_PASSWORD_HASH`). Unknown usernames are checked against a
  dummy hash, so response times do not reveal them. The session stores the user id and the
  user's `auth_version`, which is raised on password, role and status changes – this ends
  all other sessions of that user. Signed session cookie
  (HttpOnly, SameSite=Lax, Secure by default), CSRF token on every POST, login lockout after
  5 failures per client IP (in-memory, per process; behind a reverse proxy only correct when
  `FORWARDED_ALLOW_IPS` names the address the proxy connects from), strict CSP, no
  OpenAPI/docs endpoints.
- Provider tokens are sent as headers and never written into error messages or logs. A token
  entered under Platform Settings is stored in plain text in `platform_settings`; set it in
  `.env` instead if the database should hold no credentials.
- The JSON data import treats the file as untrusted: every entry is validated, a broken entry
  becomes an error message and the whole import is rolled back.
