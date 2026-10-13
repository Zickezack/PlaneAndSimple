# User stories

## Backlog

### Epic Benachrichtigungen
(Mail, account mails and price alerts are implemented – see 42–43.)
1. Price digest: daily or weekly mail per user (chosen in the personal settings) with how the
   prices of their Suchabos changed since the last digest – cheapest per month, biggest drops
   and rises, new lowest prices – optionally with the price chart as an embedded image (mail
   clients do not run JavaScript, so it must be rendered server-side, e.g. PNG – needs a new
   dependency such as Pillow or matplotlib).
2. Price alerts for Trips (complete options below a total price); today only Suchabos.

### Epic Zusatzattribute zu Verbindungen
(Layover warnings are implemented – see 41.)
1. Self-transfer / separately sold tickets within one Google itinerary – open, decision
   pending. Searched on 2026-10-14: 934 real results on 10 routes with likely low-cost
   combinations (e.g. BSL–TFS, BSL–KRK, GVA–MLA, STN–AGP) contained no itinerary mixing carriers
   outside an alliance or codeshare, so no flag could be mapped; Google does not seem to return
   such itineraries for this request and market. Needs an itinerary Google itself marks as
   "self transfer".

### Epic Internationalisierung
1. More UI languages (e.g. French, Italian, Spanish): today English and German. Needs a
   catalog per language (`locales/<code>.json`), country and currency names per language
   (generated from CLDR like the German ones) and a translation workflow for new texts.
2. Locale-aware formats per language and region instead of the fixed ones (dates, weekday and
   month names, e.g. via Babel/CLDR – a new dependency); price format with currency symbols.
   Numbers are the same in every language today (1'234.50: ' between thousands, decimal point,
   prices with the currency's decimals) – make the separators adjustable per user or locale.
3. Market per user or platform for Google Flights (`gl`/`hl` parameters: country of sale and
   language), so fares match what users in other countries see.

### Epic Datenquellen
1. Verify the Travelpayouts adapter with a real token (cabin classes, `trip_duration`, currency
   CHF) – no token available yet.

### Epic AI-gestützte Planung
(AI setup, rule-based "when to book" with AI explanation and search drafts are implemented –
see 49.)
1. Trip drafts from a plain-language wish (legs, stays, window), checked by the Trip rules and
   saved only after confirmation.
2. AI-worded price alert mails and (with the digest, Epic Benachrichtigungen 1) a digest in
   plain language: what changed and why it matters, patterns across Suchabos, the "when to
   book" advice. Numbers always from the app; every statement links to the underlying prices.
   Fixed texts (password links etc.) stay fixed.
3. "When to book" for the whole tracked search (e.g. "3 dates at their lowest price") and an
   AI weighting of the signals beyond the fixed rules.
4. Optional per-feature model choice (e.g. a cheaper model for drafts).

## Planned

(nothing at the moment)

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
41. Layover warnings next to every flight with details (flights table, flight page, chart
    panel): airport change, short connection (< 1 h, < 3 h with an airport change), overnight
    and long (≥ 5 h) layover – computed from the stored segments, so old prices get them too.
42. Mail: SMTP under Platform Settings → Mail with presets (Gmail, Outlook, iCloud, Yahoo,
    Infomaniak, GMX, Swisscom, Proton, Mailpit), password stored encrypted, test mail; Mailpit
    in the dev stack (optional in production); optional e-mail address per user; welcome mail
    (with a one-time link to set the password if the admin left it empty), "Forgot password?"
    with a one-time link (1 h), notice after every password change; mails in the user's
    language.
43. Price alerts per tracked search (optionally per route and cabin), by mail after each poll:
    at most one mail per flight and price drop, one mail per user and poll.
44. User management: admins rename users (ownership, shares and alerts follow); login with
    username or e-mail address – usernames and addresses are unique across each other.
45. Observed price range in the price chart (toggle like the arrows): per departure date a band
    from the lowest to the highest price ever observed, the line is the current price, arrows
    from the first to the current price; a ring marks dates at their lowest observed price.
46. Trip Planner: the stay (min/max days) is shown between the legs – in the form and in the
    Trip's route – so it reads as the time before the next leg; stops within a leg are explained
    as layovers.
47. Consistent numbers: ' between thousands and a decimal point in every language, counters with
    separators, every price with the currency's decimals (also in the chart; whole prices only in
    phone-width price-calendar cells); "1 passenger"; the Poll Log page is titled "Poll Log";
    neutral badges ("Deactivated") are visible on any background.
48. Flights table fits without horizontal scrolling on desktop and tablet (the flight column,
    the return line and the 1d/1w/1m changes wrap); a Trip's working options show one line per
    leg with its date, route and flight times instead of a separate first-departure column.
49. AI (optional) under Platform Settings → AI: presets for Ollama (local), any OpenAI-compatible
    API and Anthropic (Claude); API URL, model, encrypted API key, token and response limits,
    editable instructions with a built-in template, per-feature switches, connection test.
    Rule-based "when to book" advice (badge in the flights table, section on the flight page)
    with an optional AI explanation; "Describe your trip" turns a plain-language wish into a
    draft of the new tracked search form, saved only by the user.
50. Mail: every mail also as HTML (made from the same text: paragraphs, lists, clickable links);
    mail log for admins (Platform Settings → Mail: last 30 days sent/failed, latest 25 – user,
    kind, subject, error; no addresses; kept half a year). Admins send the welcome mail again
    (a new link voids older ones); "No password yet" on the user list and page.
51. AI usage log for admins (Platform Settings → AI): per feature requests, failures, tokens as
    reported by the provider and average time over 30 days, plus the latest 25 requests; no
    prompts or answers stored; kept half a year.
52. Ollama through its own API with a fixed context window (num_ctx) and keep-alive on every
    request, so a model shared with other apps is not reloaded or unloaded; the connection test
    shows how Ollama holds the model and warns about a different context window.
53. "No thinking" for Ollama (`think: false`) and, for every provider, reasoning text removed from
    answers; the top of the admins' Settings page lists the services not set up yet (flight
    data, mail server, public address, AI, Wikidata contact) with their consequences and links.
