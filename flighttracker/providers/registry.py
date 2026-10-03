from collections.abc import Callable

from flighttracker.config import Settings
from flighttracker.providers.base import FlightPriceProvider
from flighttracker.providers.google_flights import GoogleFlightsProvider
from flighttracker.providers.mock import MockProvider
from flighttracker.providers.travelpayouts import TravelpayoutsProvider


def _travelpayouts(settings: Settings) -> TravelpayoutsProvider:
    token = settings.travelpayouts_token.get_secret_value() if settings.travelpayouts_token else ""
    return TravelpayoutsProvider(token)


def _google_flights(settings: Settings) -> GoogleFlightsProvider:
    return GoogleFlightsProvider(request_delay_seconds=settings.scraper_request_delay_seconds)


PROVIDERS: dict[
    str, tuple[type[FlightPriceProvider], Callable[[Settings], FlightPriceProvider]]
] = {
    "mock": (MockProvider, lambda settings: MockProvider()),
    "travelpayouts": (TravelpayoutsProvider, _travelpayouts),
    "google_flights": (GoogleFlightsProvider, _google_flights),
}

# Names under which invented prices may be stored – marked in red wherever they are shown.
FAKE_DATA_PROVIDERS = frozenset(name for name, (cls, _) in PROVIDERS.items() if cls.fake_data)


def provider_class(name: str) -> type[FlightPriceProvider]:
    """Capabilities of a provider without instantiating it (no token needed)."""
    if name not in PROVIDERS:
        available = ", ".join(sorted(PROVIDERS))
        raise ValueError(f'Unknown FLIGHT_PROVIDER "{name}". Available: {available}')
    return PROVIDERS[name][0]


def provider_config(settings: Settings) -> tuple[str, str, float]:
    """Every setting a provider is built from – when one changes, it has to be rebuilt."""
    token = settings.travelpayouts_token.get_secret_value() if settings.travelpayouts_token else ""
    return settings.flight_provider, token, settings.scraper_request_delay_seconds


def get_provider(settings: Settings) -> FlightPriceProvider:
    provider_class(settings.flight_provider)
    return PROVIDERS[settings.flight_provider][1](settings)
