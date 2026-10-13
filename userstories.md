# User stories

## Backlog

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

### Epic Internationalisierung
1. More UI languages (e.g. French, Italian, Spanish): today English and German. Needs a
   catalog per language (`locales/<code>.json`), country and currency names per language
   (generated from CLDR like the German ones) and a translation workflow for new texts.
2. Locale-aware formats per language and region instead of the two fixed ones (numbers, dates,
   weekday and month names, e.g. via Babel/CLDR – a new dependency); price format with currency
   symbols.
3. Market per user or platform for Google Flights (`gl`/`hl` parameters: country of sale and
   language), so fares match what users in other countries see.

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
26. Pinned departure date: price over time of its flights (step line per route and stay) – "when
    to buy".
27. Price calendar (heat map) of the cheapest price per departure day, read from Google's
    calendar view (61 days per request); days with flights can be pinned from the calendar.
28. German country names in the German UI (labels and suggestions; static list from CLDR).
29. Overlap check compares what is really queried: stay lengths actually requested, the same stop
    limit and the same sampled departure days; a stricter stop limit is polled right away.
32. Trips: pause/resume, archive/restore and permanent deletion, cascading to their legs.
33. Trip options: exactly the cheapest 200 combinations (no longer cut in search order; the worker
    no longer caps paths at 1000), only current options (no past departures, only flights from the
    latest poll), and the date of every leg in the options table.
31. Modular codebase: search routes as a package, worker split by job kind, services one use case
    per module, chart script split into widgets; size rules and a feature map in architecture.md.
30. "Poll now" and the scheduler can no longer queue the same Suchabo or Trip twice (row lock).
34. Google Flights: every result sorted by price (Google's result list instead of the first ~10
    "best" flights of the page, which often missed the cheapest; retried on Google's error
    response, the page as fallback); optional return flights per Suchabo (preset in Platform
    Settings, one more request per round-trip price, counted in the request budget and shown
    with the user's limit); the tracked search page hides prices the latest poll no longer found
    (e.g. a departure day that is no longer sampled).
35. Clear the price history of a Suchabo or Trip (confirmed by name, polled again right away), e.g.
    after a change in how prices are collected.
36. Worker load for admins (Platform Settings): duration of one round of polls (measured from
    the latest polls, extrapolated for new ones) against the poll interval, reserve in time and
    requests, waiting polls, and each Suchabo's and Trip's share.
37. Calendar-driven departure days: each poll reads the price calendar first and queries half
    evenly spread and half the cheapest calendar days per month (same number of requests); the
    heat map is folded away by default. Google Flights adapter split into a package.
38. Platform currency as a dropdown of all current national currencies (ISO 4217, names in
    English and German, prices with the currency's decimals); personal time zone as a dropdown.
39. Poll schedule (admins, Platform Settings): every tracked search and Trip has a start time in
    the poll interval; new ones are placed in the largest free gap; drag and drop timeline (start
    time fields without JavaScript), "Spread evenly", expected waits; polls never overlap; a
    resumed or restored one moves to a free gap if its start is taken meanwhile. The timeline is
    the Worker load view (one card, one table) and is edited in place.
40. Quick navigation on long pages (Settings for admins, tracked search detail): a row of links
    to the sections, which are collapsible; a link focuses on its section (the others fold),
    "Show all" unfolds everything; saving returns to the section that was saved.
41. Price calendar no longer fails on Google's intermittent error response: every calendar
    request is retried like the result list (4, 8, 12 s), with a clear message if it still fails.
