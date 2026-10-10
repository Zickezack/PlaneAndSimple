"""UI labels as English source texts – templates translate them with `_()`.

Terminology: see datamodel.md (glossary).
"""

from flighttracker.domain.filters import CabinClass, TripType
from flighttracker.domain.locations import LocationKind
from flighttracker.models import (
    JobKind,
    JobStatus,
    PriceSource,
    QueryOutcome,
    SearchStatus,
    UserRole,
)

CABIN_LABELS = {
    CabinClass.ECONOMY: "Economy",
    CabinClass.PREMIUM_ECONOMY: "Premium Economy",
    CabinClass.BUSINESS: "Business",
    CabinClass.FIRST: "First",
}
TRIP_TYPE_LABELS = {
    TripType.ROUND_TRIP: "Round trip",
    TripType.ONE_WAY: "One way",
}
STOPS_OPTIONS = [
    ("any", "Any"),
    ("0", "Direct flights only"),
    ("1", "Max. 1 stop"),
    ("2", "Max. 2 stops"),
    ("3", "Max. 3 stops"),
]
LOCATION_KIND_LABELS = {LocationKind.COUNTRY: "Country", LocationKind.AIRPORT: "Airport"}
DAYS_PER_MONTH_LABELS = {
    4: "4 (about weekly)",
    8: "8 (about twice a week)",
    15: "15 (every other day)",
    31: "Every day",
}
STATUS_LABELS = {SearchStatus.ACTIVE: "Active", SearchStatus.PAUSED: "Paused"}
SOURCE_LABELS = {PriceSource.LIVE: "Live", PriceSource.BACKFILL: "Backfill"}
JOB_KIND_LABELS = {JobKind.POLL: "Poll"}
JOB_STATUS_LABELS = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RUNNING: "Running",
    JobStatus.DONE: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}
ROLE_LABELS = {UserRole.ADMIN: "Admin", UserRole.USER: "User"}

OUTCOME_LABELS = {
    QueryOutcome.OK: "Prices found",
    QueryOutcome.EMPTY: "No flights",
    QueryOutcome.FAILED: "Failed",
}

# Texts used by static/js/price-chart.js (translated server-side and passed in the chart JSON).
CHART_LABELS = {
    "aggregate": "Cheapest of {n} routes",
    "aggregateHint": "Filter by origin or destination to compare the routes one by one.",
    "previousYear": "Previous year",
    "returnOn": "Return {date}",
    "days": "{n} days",
    "direct": "direct",
    "oneStop": "1 stop",
    "stops": "{n} stops",
    "via": "via",
    "arrowHint": "Arrow: first observed price → current price",
    "firstToCurrent": "First observed price → current price",
    "invented": "invented",
    "empty": "No prices for this selection yet.",
    "lowest": "lowest",
    "chart": "Price chart. Arrow keys move between departure dates, Enter keeps one below.",
    "clickToPin": "Click to keep this date below the chart",
    "selected": "Selected: {date}",
    "clearSelection": "Clear selection",
    "details": "Details",
    "moreDates": "+{n} more dates",
    "fewerDates": "Show less",
    "historyTitle": "Price over time for this date",
    "historyHint": "One line per flight; every poll's price, so you see when it was cheapest.",
    "historyChart": "Price over time of the flights departing on {date}.",
}


def stops_label(max_stops: int | None) -> str:
    return dict(STOPS_OPTIONS)["any" if max_stops is None else str(max_stops)]


# Platform Settings: section headings and field labels/hints (services/settings.py keys).
SETTINGS_SECTION_LABELS = {
    "providers": "Flight data providers",
    "searches": "Search defaults",
    "worker": "Worker",
    "airport_import": "Airport import",
    "users": "User limits",
}
SETTINGS_FIELD_LABELS = {
    "flight_provider": "Active flight data provider",
    "travelpayouts_token": "Travelpayouts token",
    "scraper_days_per_month": "Default departure dates sampled per month",
    "scraper_request_delay_seconds": "Pause between scraper requests (seconds)",
    "default_currency": "Global currency",
    "default_poll_interval_minutes": "Global poll interval (minutes)",
    "worker_tick_seconds": "Worker tick interval (seconds)",
    "max_route_pairs_per_search": "Max. route pairs per tracked search",
    "country_default_airports": "Airports preselected per country",
    "wikidata_contact": "Wikidata contact (URL or e-mail)",
    "max_searches_per_user": "Max. tracked searches and Trips per user",
    "max_requests_per_user": "Max. provider requests per poll per user",
}
SETTINGS_FIELD_HINTS = {
    "flight_provider": "Source of flight prices; the worker switches to it on its next tick.",
    "travelpayouts_token": (
        "Free token from travelpayouts.com. Leave blank to keep the current value."
    ),
    "scraper_days_per_month": (
        "Suggested default when creating a new tracked search; each one can change it."
    ),
    "scraper_request_delay_seconds": "Pause between Google Flights requests, to stay polite.",
    "default_currency": (
        "Currency requested from the provider for every search and Trip. Stored prices keep "
        "the currency they were observed in."
    ),
    "default_poll_interval_minutes": (
        "How often all active tracked searches and Trips are polled. 1440 minutes is 24 hours."
    ),
    "worker_tick_seconds": (
        "How often the worker checks for due polls when not woken immediately."
    ),
    "max_route_pairs_per_search": (
        "Upper limit so a broad country selection cannot explode into too many routes."
    ),
    "country_default_airports": (
        "How many of a country's largest airports are preselected by passenger numbers."
    ),
    "wikidata_contact": (
        "Sent as the Wikidata User-Agent contact; required for passenger numbers to be "
        "fetched at all."
    ),
    "max_searches_per_user": (
        "Counts active and paused ones; archived ones are free. A Trip counts once. "
        "Admins have no limit; each user can get an own limit under User management."
    ),
    "max_requests_per_user": (
        "Provider requests of one poll of all of a user's tracked searches and Trips "
        "together – the real cost. 600 take about 40 minutes with a 3 s pause."
    ),
}
