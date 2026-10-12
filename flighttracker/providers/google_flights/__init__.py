"""Google Flights adapter based on the open-source scraper `fast-flights` (extra `scraper`).

Live fares, no history. Scraping may break whenever Google changes its page and is a
Terms-of-Service grey area – keep request rates low and treat this adapter as best effort.

Fares come from the request behind Google's result list ("GetShoppingResults"), sorted by
price with every result – the result page alone only holds the first ~10 "best" flights and
often misses the cheapest. If that request fails, the result page is read instead
(`parse_fares`). fast-flights builds the page URL and provides the HTTP client. From European
IP addresses Google first shows a cookie consent page instead of the results, so requests
carry a consent cookie (rejecting all optional cookies).
"""

from flighttracker.providers.google_flights.common import FareOption, FareRequest, SelectedLeg
from flighttracker.providers.google_flights.page import fast_flights_search, parse_fares
from flighttracker.providers.google_flights.price_calendar import (
    CALENDAR_DAYS_PER_REQUEST,
    calendar_request_body,
    google_calendar_search,
    parse_calendar,
)
from flighttracker.providers.google_flights.provider import GoogleFlightsProvider
from flighttracker.providers.google_flights.results import (
    google_flights_search,
    google_return_search,
    parse_shopping,
    selected_legs,
    shopping_request_body,
)

__all__ = [
    "CALENDAR_DAYS_PER_REQUEST",
    "FareOption",
    "FareRequest",
    "GoogleFlightsProvider",
    "SelectedLeg",
    "calendar_request_body",
    "fast_flights_search",
    "google_calendar_search",
    "google_flights_search",
    "google_return_search",
    "parse_calendar",
    "parse_fares",
    "parse_shopping",
    "selected_legs",
    "shopping_request_body",
]
