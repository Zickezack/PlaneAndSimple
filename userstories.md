# User stories

## Backlog

### Epic Architecture
1. Modularize the code of the app --> Efficiency in AI

### Epic Deployment
1. Optional automation of `scripts/deploy.sh` (e.g. GitHub Actions via SSH on push to `main`;
   `dev` → staging).
2. Secret scanning (gitleaks) as pre-commit hook and in CI.
3. Container hardening in `docker-compose.yml`: `security_opt: [no-new-privileges:true]`,
   `cap_drop: [ALL]`, read-only root filesystem where possible.

### Epic Suchabos & Auswertung
1. Second chart: price development over time for chosen departure dates ("when to buy").
2. Heat-map calendar of the cheapest departure dates (like booking aggregators); ideally fed by
   Google's calendar data (all days of a month in one request) instead of per-day requests.
3. German country names (OurAirports only provides English names; the German UI shows them in English).
4. Overlap check must match what is really queried: an empty stay counts as "unlimited" in
   `domain/coverage.py` but is queried as 7 days only, and "any stops" is treated as covering
   "direct only" although only the cheapest itinerary per day is stored. Today a new Suchabo can
   be reported as "already covered" while its prices are never fetched.
5. "Poll now" and the scheduler can queue the same Suchabo twice at the same moment
   (`services/jobs.py` checks for an open poll without a lock).

### Epic Trip Planner
1. Pause, archive and delete Trips (cascading to their leg Suchabos); today a Trip polls until
   its window ends and its leg Suchabos cannot be changed individually.
2. Cheapest options first: `find_itineraries` stops after the first 200 combinations and sorts
   only those, so the cheapest one can be missing (`connect_quotes` likewise stops at 1000 paths).
3. Show only current options: hide departures in the past and combinations not seen in the latest
   poll.

### Epic Benutzerverwaltung
1. Rename users (today only possible by creating a new user and changing the owner).

### Epic Benachrichtigungen
1. Mail delivery via SMTP, with Mailpit as the server in the dev stack (and as an optional
   self-hosted catcher): account mails (welcome mail on creation, self-service password reset
   with a one-time, expiring link, notice after a password change) and Suchabo messages.
   Needs an e-mail address per user (personal data, see datamodel.md) and SMTP settings in
   `.env` / Platform Settings; mails in the user's language.
2. Price alerts per Suchabo: the user sets a price (per Suchabo, optionally per route or
   cabin) below which a message is sent; at most one message per flight and price drop, so
   repeated polls do not spam.
3. Price digest: daily or weekly mail per user (chosen in the personal settings) with how the
   prices of their Suchabos changed since the last digest – cheapest per month, biggest drops
   and rises, new lowest prices – optionally with the price chart as an embedded image (mail
   clients do not run JavaScript, so it must be rendered server-side, e.g. PNG).

### Epic AI-gestützte Planung
1. Optional Ollama-assisted drafting: turn plain-language travel intent into a search or Trip
   draft, validate it with existing deterministic rules, and require confirmation before saving
   or polling. AI is not a fare source and does not replace airport suggestions.
2. "When to book" decision: the price history per flight (first price, curve, current price) is
   the basis for a booking recommendation, per flight or for the whole Suchabo (open). A
   rule-based first version works without AI: compare the current price with the flight's own
   minimum/average and its trend, plus days to departure ("lowest seen", "above average",
   "rising"). AI could later weigh these signals and explain the advice.
3. AI-written price digest and alerts (builds on Epic Benachrichtigungen 1–3): summarise in
   plain language what changed and why it matters ("Barcelona in May is 18 % below last
   year's level, the cheapest it has been in 6 weeks"), point out patterns across Suchabos
   (weekday effects, routes that move together) and add the "when to book" advice. The numbers
   always come from the deterministic digest; AI only phrases and prioritises them, and every
   statement links to the underlying prices.

## Planned

1. Verify the Travelpayouts adapter with a real token (cabin classes, `trip_duration`, currency CHF).

## Implemented

1. Project structure, Docker Compose stack (db, migrate, web, worker), devcontainer with Claude Code.
2. Database schema with append-only price history, revisions and soft delete (migration `0001`).
3. Admin login from env (scrypt hash, CSRF, login lockout, security headers).
4. Suchabos: create, edit (new revision, history kept), pause/resume, archive/restore, delete permanently.
4. Overlap check when creating: "bereits abgedeckt" or "zu bestehendem Suchabo hinzufügen" (filters are merged).
6. Worker: regular polling (new or broadened Suchabos are polled right away), retries.
   The initial backfill was removed – no free source offers historical fares.
7. Provider adapters: mock, Travelpayouts, Google Flights (fast-flights, with consent cookie;
   verified against live results on 29.09.2026).
8. Month overview with previous-year comparison (table).
9. Country locations: the largest airports (Wikidata passenger numbers) are preselected, the
   selection stays visible and editable per Suchabo, incl. the number of queries per poll.
10. Dev stack with live reload (`web`/`worker` restart on code or `.env` changes).
11. Manual deployment from GitHub with `scripts/deploy.sh` (no Portainer).
12. English UI with German as second language (switch in the top bar).
13. Suggestions while typing for airports (IATA), countries (ISO) and currencies; "Clear selection"
    per country airport list.
14. Children (2–11) as passengers, with a note when the data source cannot price them.
15. Invented data (provider `mock`) is marked in red everywhere (banner, price rows, log).
16. Global persistent query log (menu "Poll Log") with filters by Suchabo and result.
17. New Suchabos are polled immediately (worker wake-up via PostgreSQL NOTIFY); single failed
    queries no longer fail a whole poll; interrupted polls resume where they stopped.
18. Price chart per Suchabo (price by departure date, one line per route, previous year, tooltips)
    with filters by origin, destination and cabin; tables show the flight behind every price
    (dates, airlines, flight numbers, times, duration, stops/layovers, link to Google Flights).
19. Timestamps in Zurich time; sampled departure dates per month and several stay lengths per Suchabo
    (with request/time estimate); chart: pin a date, stay filter; table: cheapest per month
    unfolding into all dates, or all flights; log entries unfold into the dates found.
20. Simpler detail page (summary line, chart, lean table; settings/history/deletion folded away),
    flight detail page with Flightradar24 links and the price history of the dates, "Poll now"
    button, colour dot per table row matching the chart line, versioned static files.
21. Main page explains the app and how to create a tracked search (folds away once one exists).
   Platform Settings page (admin only): global JSON export/import includes Suchabos and Trips;
   live overrides include platform currency and polling cadence, with import remaining in the
   same place. Navigation orders primary links and places Language, Poll Log and Settings at right.
22. Trip Planner: create a bounded 2–8-leg itinerary from one-way Suchabos, with airport/country
   suggestions, reviewed country airport selections, shared cabin/stops/adult/child criteria and
   calendar-day stays. Coordinated exact-date polls join only matching airports and feasible
   dates. Complete options require provider flight times; totals are separate fares, not a
   through-ticket price.
23. User management: database users with roles admin/user (env admin stays as fallback),
    own and shared (view / view and edit) tracked searches and Trips, per-user limits on
    tracked searches and provider requests per poll, personal settings (language, time zone,
    password); Platform Settings for admins only.
24. Poll Log shows running and queued polls; they can be cancelled.
25. Prices are stored with their passengers; a changed passenger number is never shown as a
    price change.
