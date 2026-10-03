from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read exclusively from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str

    secret_key: SecretStr = Field(min_length=32)
    admin_username: str = Field(min_length=1)
    admin_password_hash: SecretStr
    session_cookie_secure: bool = True

    flight_provider: str = "mock"
    travelpayouts_token: SecretStr | None = None
    # Default monthly departure-date sampling for new Suchabos (adjustable per Suchabo).
    scraper_days_per_month: int = Field(default=4, ge=1, le=31)
    scraper_request_delay_seconds: float = Field(default=3.0, ge=0, le=60)

    worker_tick_seconds: int = Field(default=30, ge=1, le=3600)
    default_poll_interval_minutes: int = Field(default=1440, ge=15, le=43200)
    max_route_pairs_per_search: int = Field(default=50, ge=1)
    # Largest airports preselected when a whole country is entered as origin/destination.
    country_default_airports: int = Field(default=3, ge=1, le=20)
    # Contact (URL or e-mail) sent to Wikidata; passenger numbers are only fetched when set.
    wikidata_contact: str = ""
    default_currency: str = Field(default="CHF", pattern=r"^[A-Z]{3}$")
    # Timestamps are stored in UTC and shown in this time zone.
    display_timezone: str = "Europe/Zurich"

    @field_validator("secret_key")
    @classmethod
    def _not_the_example_key(cls, value: SecretStr) -> SecretStr:
        # The `.env.example` placeholder is public; a key anyone knows lets them forge sessions.
        if value.get_secret_value().startswith("change-me"):
            raise ValueError("SECRET_KEY is still the example placeholder: openssl rand -hex 32")
        return value

    @field_validator("display_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone: {value}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
