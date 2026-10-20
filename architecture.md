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
- **Price calendar first, then the fares:** a poll of a provider with a calendar
  (`calendar_days_per_request`, Google Flights: 61 days) first fetches the cheapest price of
  every departure day from tomorrow to the end of the last polled month – one query per route,
  cabin and stay length, split into requests by the provider; stored in `price_calendar`, the
  requests count towards the user's request budget. Google answers about every third calendar
  request with its intermittent error response; like the result list, each request is repeated
  after 4, 8 and 12 s (`google_flights/common.with_retries`) – in tests always successful by the
  third attempt. Only a request still failing after that is logged as failed.
- **Which departure days:** for a provider that queries single days *and* has a calendar
  (Google Flights), each fare query asks for half evenly spread days (the same every poll – a
  continuous price history and previous-year comparison) and half the cheapest days of the
  calendar, over the stay lengths asked for (`domain/sampling.choose_days`,
  `services/day_selection.py`). Same number of requests as pure spreading; without calendar
  prices for a month its days stay evenly spread. 1 day per month is the cheapest one, every
  day stays every day.
- **One commit per query:** a poll job consists of provider queries (route × departure month ×
  cabin). Each query stores its prices and a `query_log` entry and is committed on its own.
  A failed query does not fail the job; the job only fails (and is retried) if all queries fail.
- **Restarts:** a stop request is honoured between two queries (the job goes back to the
  queue). At start the worker requeues jobs left `running` by a killed process without counting
  the attempt. A resumed job skips the queries it already completed.
- **Global search settings:** all active Suchabos and Trips use the same platform poll interval
  and provider currency (a dropdown of all current national currencies, ISO 4217 from CLDR,
  `domain/currencies.py`; prices show the currency's decimals). Historical prices keep their
  recorded currency; there is no conversion.
- **Poll schedule:** every active Suchabo and Trip has a planned start inside the poll interval
  (`poll_offset_minutes`, counted in local wall-clock time of `DISPLAY_TIMEZONE` – with 24 h a
  time of day, stable across DST; `domain/poll_plan.py`). Each worker tick first places those
  without one (new, imported, resumed) in the middle of the largest free gap, using the Worker
  load durations (`services/poll_plan.py`). After a poll is queued, `next_poll_at` is the next
  planned start at least half an interval later, so an extra poll (new, broadened, "poll now")
  is not followed by the planned one minutes later. Platform Settings → "Worker load and poll
  schedule" shows the schedule as timeline – it is the worker load – with the load figures and
  one table (start, requests, duration, share, expected wait); "Edit schedule" switches the card
  to editing in place (drag and drop bars, start time fields, "Spread evenly"; without
  JavaScript `/settings?edit=schedule`);
  a changed interval scales all starts proportionally. A resumed or restored one keeps its start
  unless it would overlap another poll; then it moves to the largest free gap (`fit_returning`). Polls never run at the same time: the
  single worker runs one job after the other, a start that comes during another poll waits
  (the page shows the expected wait).
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

**Module size and cut (for humans and AI alike: read few, small, cohesive files):**
- Aim for at most ~300 lines per Python module and ~400 per JS file. Split by **feature**, not by
  technical kind, before a file grows past that.
- A route module serving several sub-features becomes a package `web/routes/<feature>/`. Its
  `__init__.py` only builds `router` in an explicit order – catch-all routes last (FastAPI
  matches in registration order; `tests/unit/test_route_order.py` guards the searches package).
  Helpers shared inside the package live in `_common.py`.
- Form parsing: one module per feature (`web/forms.py` for Suchabos, `web/trip_forms.py` for
  Trips, `web/settings_forms.py` for the Settings page); routes only call them and render.
- Worker: `__main__.py` is only the entry point; `loop.py` (scheduling, dispatch), `wake.py`
  (LISTEN/NOTIFY), `job_control.py` (stop/cancel checks), one module per job kind
  (`trip_job.py`), `after_poll.py` (the steps after a completed poll). Rules about *what* to
  query and store live in services (`ingestion.py`, `trip_polling.py`, `poll_reports.py`).
- Services: one use case per module – write side `searches`, `trips`; lookups `locations`;
  read side `history`, `price_trends`, `month_overview`, `capacity`; polling `ingestion`, `day_selection`, `trip_polling`;
  transfer `data_export`, `data_import`; accounts `users` (management), `login`, `password_tokens`;
  mail `mail`, `mail_messages`, `price_alerts`; AI `ai` (one request), `ai_wire` (the providers'
  wire formats), `ai_log` (usage log), `ai_prompts`, `ai_summaries` (what the worker asks by
  itself); after a poll `poll_reports` (with `report_texts` for its words); admins' logs read
  side `usage_logs`.
- No imports of another module's `_private` names: a helper a second module needs becomes public.
- JS: one file per widget (chart, `price-band`, `price-history`, `price-calendar`, `info` for the "i" explanations), shared SVG helpers in `static/js/chart-svg.js` (`window.PlaneChart`);
  classic `defer` scripts loaded via `static_url()` in a fixed order – no ES-module `import`
  (imported files would skip the `?v=` cache busting).

**Where is what:**

| Feature | Files |
|---|---|
| Tracked searches list | `web/routes/searches/listing.py`, `templates/searches/list.html` |
| Create/edit form | `web/routes/searches/form.py`, `web/forms.py`, `services/searches.py`, `services/locations.py` |
| Detail page, chart, heat map | `web/routes/searches/detail.py`, `web/flights.py`, `services/history.py`, `services/price_trends.py`, `static/js/price-chart.js` (+ `price-history.js`, `price-calendar.js`, `chart-svg.js`) |
| Poll now, pause, archive, delete | `web/routes/searches/actions.py`, `services/jobs.py` |
| Sharing, owners, quotas | `web/routes/searches/shares.py`, `web/sharing.py`, `services/access.py`, `services/quota.py` |
| Platform Settings sections (save, test) | `web/routes/settings_actions.py`, `web/settings_sections.py`, `web/settings_forms.py`, `static/js/settings-sections.js`, `templates/settings/index.html` + `_setup.html` |
| Worker load (Platform Settings) | `services/capacity.py`, `web/routes/settings.py`, `templates/settings/index.html` (one card with the poll schedule) |
| Poll schedule (Platform Settings) | `domain/poll_plan.py`, `services/poll_plan.py`, `web/routes/schedule.py`, `web/schedule_view.py`, `templates/settings/index.html` + `_timeline.html`, `static/js/poll-schedule.js`; scheduler in `services/jobs.py` |
| Trips | `web/routes/trips.py`, `web/trip_forms.py`, `services/trips.py`, `domain/trips.py` |
| Polling | `worker/*`, `services/ingestion.py`, `services/day_selection.py`, `domain/sampling.py`, `services/trip_polling.py`, `providers/*` (Google Flights: package `providers/google_flights/` – `results` list, `page` fallback, `price_calendar`, `provider`) |
| Import/export | `services/data_export.py`, `services/data_import.py` |
| Mail, password links | `services/mail.py`, `services/mail_messages.py`, `services/password_tokens.py`, `web/mails.py`, `web/routes/password.py`, `security/secrets.py` |
| Price alerts | `services/price_alerts.py`, `web/routes/searches/alerts.py`, worker step in `worker/after_poll.py` |
| What a poll changed (report, AI summary) | `domain/poll_report.py`, `domain/deals.py`, `services/poll_reports.py`, `services/report_texts.py`, `services/ai_summaries.py`, `worker/after_poll.py`, `templates/searches/_poll_report.html` |
| Deal radar (unusually cheap prices) | `domain/deals.py`, `services/deal_radar.py`, `services/mail_messages.py` (`deal_mail`), worker step in `worker/after_poll.py` |
| Layover warnings | `domain/layovers.py`, `web/flights.py`, macros `layover_warning` / `layover_place` |
| AI: setup, drafts, "when to book" | `domain/ai_providers.py`, `domain/booking_advice.py`, `services/ai.py`, `services/ai_wire.py`, `services/ai_prompts.py`, `web/ai_report.py` (connection test), `web/routes/searches/draft.py`, `web/routes/searches/advice.py` |
| AI usage log | `services/ai_log.py` (write), `services/usage_logs.py` (read), `web/routes/ai_usage.py` + `templates/settings/ai_request.html` (one request stage by stage) |
| "i" explanations | macros `ui.info` / `ui.summary_info` and the `hint`/`note` of the field macros in `templates/_macros.html`, `static/js/info.js` |

**Plane and simple (UI principle):** every page shows the essentials first and folds the rest
away. Suchabo detail: summary line, "Poll now", chart and a lean flights table; settings, recent
polls, change history and deletion are `<details>` sections; everything about one flight lives on
its flight detail page (`/searches/<id>/flights/<price id>`: segments with Flightradar24 links,
layovers, price history of these dates, Google Flights link). Filters appear only when they offer
a real choice; rarely changed form fields sit under "More options". New features follow this.
Explanations of a field or function sit behind an "i" (macro `ui.info`, right after the label,
heading or button it explains; the field macros take `hint=` for it): hover, keyboard focus or a
tap shows the text, Escape or a click elsewhere closes it, and screen readers read it right
after the button. In the `<summary>` of a folding section it follows the title and its badges
(`ui.summary_info`: a click on it never folds the section, and its text is the button's
description instead of part of the summary's name). Hover and focus work
without JavaScript (`static/js/info.js` adds tapping and keeps the bubble inside the window).
What the user must not miss stays visible: requirements ("At least 12 characters"), warnings
and status (`note=` of the field macros, `<p class="note">`).
Pages with many sections (Settings for admins, Suchabo detail – from about five) get a quick
navigation row (macro `ui.section_nav`, behaviour in `static/js/app.js`) above cards that are
`<details>` with an `id`: a link opens its section and folds the others, "Show all" unfolds them
all, and a `#section` in the URL opens it on load – so saving redirects to `…#section`. Without
JavaScript the links are plain anchors.

**Static files** are referenced with `static_url()`, which appends the file's modification time,
so browsers pick up CSS/JS changes immediately.

**UI languages:** English (default) and German, switchable per session (`/language/<code>`).
Write every UI text in English: `{{ _("Text {n}", n=…) }}` in templates, `Msg("Text {n}", n=…)`
for messages created in Python (validation errors, flash messages – rendered per request), label
tables in `web/labels.py`. Add the German translation to `flighttracker/locales/de.json` (keyed
by the English text); `tests/unit/test_i18n.py` fails on missing or stale entries. Country
names come from OurAirports in English; German ones are in `flighttracker/locales/countries.de.json`
(ISO code → name, generated once from CLDR), used for labels and for matching suggestions.
Currency names likewise: English in `domain/currencies.py`, German in
`flighttracker/locales/currencies.de.json`. Personal time zone: dropdown of all IANA zones.
Numbers look the same in every language: `i18n.format_number` (1'234.50 – ' between thousands,
decimal point); integer parameters of translated texts get it automatically, raw counters use
the `number` filter, prices the currency's decimals (`price` filter, chart). Logs, CLI output and
stored error texts (`fetch_jobs.last_error`) are English only.

**Price chart:** `static/js/price-chart.js` draws the detail page's chart as plain SVG (no
library) from JSON embedded in the page (`web/flights.py` → `<script type="application/json">`,
escaped so it cannot close the tag). One line per route in the validated categorical palette
(colour follows the route); more than 8 routes collapse into one "cheapest of n routes" line;
previous-year values appear as a gray context line when a single line is shown. The **observed
price range** (`static/js/price-band.js`, checkbox like the arrows) shades, for a single line,
the lowest to highest price ever observed for each departure date (from `hist`) around the line
of the current prices; a ring marks a date that is at its lowest observed price now. With
several lines it asks to filter to one. A click pins a
date (diamond markers + a panel with that day's flights and links). The panel also draws that
date's price over time (x = poll date, a step line per flight, from `hist`/`seen` in the JSON),
so the user sees when a flight was cheapest. Below, a **price calendar** (heat map) shows the
cheapest calendar price of every departure day for the same filters (sequential blue, light =
cheaper, price printed in every cell, cheapest day ringed), folded away by default – the
chart already shows the cheapest calendar days with their flights; days that also have flights
in the chart can be clicked to pin them. The filter row (origin,
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
| `google_flights` | Google Flights: the request behind its result list (`GetShoppingResults`, every result sorted by price, read by `parse_shopping`); the result page as fallback (`fast-flights`, pinned, builds the URL; our `parse_fares` reads it) | **Recommended, no account needed.** Live fares, one request per sampled departure date × stay length (dates per month set per Suchabo; every stay length from min. to max., at most 8; a failed request is skipped). The result page alone only holds the first ~10 "best" flights and often misses the cheapest, so it is only read when the result list keeps answering with Google's intermittent error response (retried after 4, 8 and 12 s). Round trips are listed by their outbound flight with the cheapest round-trip total; for a Suchabo with "Load return flights" (More options; the Platform Setting is only the preset for new ones) one more request per round-trip price names that outbound flight and stores the cheapest return flight (`details.return`, counted in the request budget). Sends a consent cookie, otherwise EU/CH requests only get Google's cookie page. Skips itineraries without a price. Can break whenever Google changes its format; a Terms-of-Service grey area – keep request rates low. |
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

Options are **exact and current**: `find_itineraries` returns the 200 cheapest combinations
(legs tried cheapest first, a branch is cut once its price plus the cheapest possible rest cannot
beat the 200th), and only flights seen in the Trip's latest completed poll and departing today or
later count. The worker keeps one path per last flight and currency (all a following leg depends
on) instead of capping paths, so no candidate date is lost.

**Lifecycle:** pause/resume (editors) and archive/restore/delete (owner and admins) act on the
Trip and its leg Suchabos together (`services/trips.py`); legs cannot be changed on their own.
Restoring is checked against the owner's quota; deleting and clearing the price history (all
legs, then polled again) ask for the exact name.

## Users and permissions

- **Platform Settings sections** are forms with their own actions (`web/routes/settings_actions.py`,
  form logic in `web/settings_sections.py`): Save, and for Mail "Check the login" / "Send a test
  mail", for AI "Test the connection" – the tests use the values as entered, nothing has to be
  saved first. Every field is validated on its own (translatable messages); a wrong field is
  marked and nothing is stored, but nothing typed is lost. A provider preset fills only fields
  that are empty or still show the stored value – edits always win. With JavaScript
  (`static/js/settings-sections.js`) the actions answer with JSON: results appear below the
  buttons, errors at the field, the setup status is refreshed, the page neither reloads nor
  moves. Without it a save redirects back to the section; a test or a failed save shows the
  page again with the entered values.
- **Setup status:** the top of the admins' Settings page lists the platform services that are
  not set up yet – needed (real flight data), recommended (mail server; public address once mail
  is on), optional (AI, Wikidata contact) – with what is missing and a link to the section
  (`web/settings_forms.setup_status`). A mail server counts as set up only with username and
  password; without them it is most likely a test catcher (Mailpit) or a local relay. "Check the
  login" (and saving the Mail section) connects and logs in without sending (`mail.check_login`).
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
  deactivated or deleted in the UI; no admin can demote, deactivate or delete themselves either.
- **Usernames and login:** admins rename database users (also themselves) on the user page;
  everything refers to the user id, so ownership, shares and alerts follow. Users log in with
  their username or their e-mail address (both ignoring case). A username never equals another
  user's address and vice versa, so every login names exactly one user (`services/login.py`).

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
  poll; for Google Flights each query is `departure dates per month × stay lengths` requests
  (form and detail page show both counts and an estimated duration). A country only expands to its **selected**
  airports: the `COUNTRY_DEFAULT_AIRPORTS` largest (Wikidata passenger numbers) are preselected
  and shown as checkboxes before saving; routes are capped by `MAX_ROUTE_PAIRS_PER_SEARCH`.

## Mail

Optional; "on" once SMTP host and sender are set (`.env` or Platform Settings → Mail, with
provider presets in `domain/mail_providers.py`). `services/mail.py` sends with `smtplib`
(STARTTLS, SSL or none; 15 s timeout) a text part and an HTML part made from the same text
(paragraphs, "• " lists, clickable links, inline styles); texts in `services/mail_messages.py`,
in the recipient's language. `mail.deliver` logs every attempt in `mail_log` (user id, kind,
subject, error – no address; pruned after half a year), shown to admins in the Mail section;
the caller commits. Account mails are sent from the web request that triggers them
(`web/mails.py`): welcome mail, "Forgot password?" and the notice after any password change.
Price alerts (`services/price_alerts.py`) are checked by the worker after every completed poll
of a Suchabo; a failure is logged and never fails the job. Links use `PUBLIC_BASE_URL`, in the
web also the request's address. Dev stack: every mail goes to Mailpit (web view on the
forwarded port 8025); production: optional `mailpit` compose profile.

**Price alerts:** per user and Suchabo, a highest price in the platform currency, optionally
one route and/or cabin. Only current, upcoming, real (not invented) prices for the Suchabo's
passengers count. A flight (route, cabin, departure and return date) is reported once and again
only below the price last reported (`price_alert_notifications`); it counts as reported only
after its mail went out, so a failed mail is retried after the next poll.

## AI (optional)

Off until a provider is set (`.env` `AI_*` or Platform Settings → AI, presets in
`domain/ai_providers.py`). `services/ai.py` checks and sends one request; `services/ai_wire.py`
speaks the three wire formats over plain `httpx` (no provider SDK, no new dependency): Anthropic's Messages API (`POST /v1/messages`, JSON drafts via
`output_config.format`), Ollama's own chat API (`POST /api/chat`, JSON schema in `format`) and the
OpenAI-compatible chat completions API (OpenAI, LM Studio, Mistral, Groq, OpenRouter …; JSON
mode). For Ollama every request carries the configured context window (`options.num_ctx`) and
`keep_alive`: a model shared with other apps (e.g. loaded with 8192 tokens and `-1` = forever)
is then neither reloaded nor unloaded early – a different `num_ctx` makes Ollama reload it, a
missing `keep_alive` resets its lifetime to 5 minutes. The answer is capped to what fits next to
the prompt in that window; the connection test reads `/api/ps` and warns about a mismatch.
"No thinking" (Ollama only) sends `think: false`; for every provider, reasoning a model writes
into its answer (`<think>…</think>`) is removed before a draft is parsed or an explanation shown.
Thinking returned separately (Ollama `message.thinking`, `reasoning_content`/`reasoning` of
OpenAI-compatible servers, Anthropic's thinking blocks) only goes to the usage log. Synchronous
in the web request, 90 s timeout. The token limit is a safeguard of the app (estimated at 3.5
characters per token, never sent): longer requests are not sent. The response limit is sent
(`max_tokens`; for Ollama `num_predict`, at most what the context window leaves next to the
prompt; thinking models count their thinking); an answer that hits it (`max_tokens` / `length`
as stop reason) is cut off and fails as such. A draft whose answer is no JSON object fails too.
The API key is stored Fernet-encrypted like the SMTP password; errors never contain it.

What the AI is asked lives in `services/ai_prompts.py`: an editable system prompt (built-in
template as default) plus fixed guardrails – use only the given facts, never invent numbers,
answer in the user's language. The AI never calculates and never saves anything:
- **"When to book"** is rule-based (`domain/booking_advice.py`: lowest price seen, below/above
  average, rising/falling since the poll before, departure within 21 days → book / wait / watch)
  and shown without AI as a badge in the flights table and as a section on the flight page;
  "Explain with AI" there only puts the same facts into sentences.
- **Draft from a wish** ("Describe your trip" on the new tracked search page): the model returns
  a JSON draft that fills the form – place names are looked up as airports/countries, values
  clamped – and the user saves it through the normal form with all its validation.
With a cloud provider the facts of a request (routes, dates, prices; for a draft the travel
wish – no account data) leave the server; a local Ollama keeps them.

**Usage log** (`services/ai_log.py`, admins only): every request – failed and cut-off ones too –
is a row in `ai_usage` with feature, user, model, tokens as reported by the provider, the
answer limit, duration, outcome (OK / cut off / failed) and stop reason, plus what went out
(address without credentials, body without the prompts – never a header) and the rest of the
provider's response (timings, ids, token details). While "Log messages" is on (default) it also
keeps the messages: system prompt, request, thinking, the answer as received and as the app
used it (pretty JSON for a draft) – cleared after 30 days, the row stays half a year. The AI
section summarises the last 30 days per feature (`services/usage_logs.py`); a request's time
opens `/settings/ai-usage/<id>` with the four stages – sent by the app, thinking, received (for
Ollama with its timings; a model load over a second means it was (re)loaded for the request),
used by the app.

## After a poll

After every completed poll of a tracked search the worker runs a few steps
(`worker/after_poll.py`), each on its own with its own commit – a failing AI provider or mail
server never fails the poll and never stops the next step:

1. **Report** (`services/poll_reports.py`, rules in `domain/poll_report.py`): the real, upcoming
   prices the poll observed (fake data left out; the currency most of them have) are compared
   with the prices observed before it – flights compared, cheaper and dearer by 3 % or more,
   new ones, the cheapest flight now against the cheapest of the poll before (from that poll's
   report), the cheapest per departure month, new lowest prices (3 % or more below everything
   seen before for the flight), the biggest drops and rises, and unusually cheap prices
   (`domain/deals.py`: at most 60 % of the median price of the same route and cabin over at
   least 8 departure dates of the same poll). Stored as versioned JSON in `poll_reports`, one
   row per job, kept 90 days. **Notable** is a poll with an unusually cheap price, a cheapest
   price that moved by 3 % or more, a new lowest price, or 25 % of the compared flights
   changed; a first poll only with an unusually cheap price.
2. **AI summary** (`services/ai_summaries.py`) of a notable report – only with AI set up,
   "Summaries after polls" on, and not within 15 minutes after a failed request of the worker
   (a server that does not answer must not slow every poll down by the timeout). The prompt
   holds only the report's facts and the usual guardrails; the answer, in the owner's
   language, is stored with the report (feature `summary` in the usage log).
3. **Deal radar mails** (`services/deal_radar.py`): the report's unusually cheap prices go to
   the owner and everyone the tracked search is shared with – active, with an e-mail address
   and "Mails about unusually cheap prices" on (personal settings, default on). A flight is
   mailed to a user once and again only when it gets cheaper (`deal_notifications`; it counts
   as mailed only after its mail went out). The flights table marks them "Unusually cheap".
4. **Price alert mails** (see Mail): after a notable poll they carry its summary – the AI's
   words when they are in the recipient's language, otherwise the report's first lines; deal
   mails carry only the AI's words.

The tracked search's page shows the latest report as "Since the poll before" (the AI's words
only in their own language, the facts always, as translated lines from
`services/report_texts.py`); the list shows one line per tracked search (cheapest price and
its change, new lowest and unusually cheap prices). Clearing the price history clears the
reports too.

## Security

- No secrets in the repository or image: `.env` is git- and docker-ignored; `alembic.ini`
  contains no URL; migrations contain schema only. The production compose aborts if required
  variables are missing, and the app refuses the `.env.example` placeholder `SECRET_KEY`.
- Logins: users in the database (scrypt hashes, minimum 12 characters) plus the env admin
  (`ADMIN_USERNAME`, scrypt `ADMIN_PASSWORD_HASH`), by username or e-mail address. Unknown
  logins are checked against a dummy hash, so response times do not reveal them. The session stores the user id and the
  user's `auth_version`, which is raised on password, role and status changes – this ends
  all other sessions of that user. Signed session cookie
  (HttpOnly, SameSite=Lax, Secure by default), CSRF token on every POST, login lockout after
  5 failures per client IP (in-memory, per process; behind a reverse proxy only correct when
  `FORWARDED_ALLOW_IPS` names the address the proxy connects from), strict CSP, no
  OpenAPI/docs endpoints.
- Password links: random 32-byte tokens, only their SHA-256 stored (`password_tokens`), valid
  once and 1 hour (reset) or 7 days (welcome); a new link or a set password voids the older
  ones. "Forgot password?" answers the same for every input and shares the login lockout per
  client; the `.env` admin cannot reset by mail.
- The SMTP password entered under Platform Settings is stored Fernet-encrypted with a key
  derived from `SECRET_KEY` (`security/secrets.py`); after a key change it is ignored and
  must be entered again. SMTP errors never contain credentials.
- Provider tokens are sent as headers and never written into error messages or logs. A token
  entered under Platform Settings is stored in plain text in `platform_settings`; set it in
  `.env` instead if the database should hold no credentials.
- The JSON data import treats the file as untrusted: every entry is validated, a broken entry
  becomes an error message and the whole import is rolled back.
