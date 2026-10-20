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
    # Default for new round-trip Suchabos: also load the return flight of every price.
    scraper_return_flights: bool = False

    worker_tick_seconds: int = Field(default=30, ge=1, le=3600)
    default_poll_interval_minutes: int = Field(default=1440, ge=15, le=43200)
    max_route_pairs_per_search: int = Field(default=50, ge=1)
    # Largest airports preselected when a whole country is entered as origin/destination.
    country_default_airports: int = Field(default=3, ge=1, le=20)
    # Contact (URL or e-mail) sent to Wikidata; passenger numbers are only fetched when set.
    wikidata_contact: str = ""
    default_currency: str = Field(default="CHF", pattern=r"^[A-Z]{3}$")
    # Quota per (non-admin) user: Suchabos + Trips, and provider requests per poll of all of
    # them together. The request budget is what actually costs: 600 requests take about
    # 40 minutes of scraper time per poll with the default 3 s pause.
    max_searches_per_user: int = Field(default=10, ge=0, le=1000)
    max_requests_per_user: int = Field(default=600, ge=0, le=1_000_000)
    # Mail (SMTP); all can also be set under Platform Settings → Mail. Without host and sender
    # no mails are sent and the mail features stay hidden.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    # "starttls" (port 587), "ssl" (port 465) or "none" (local catcher such as Mailpit).
    smtp_security: str = Field(default="starttls", pattern=r"^(starttls|ssl|none)$")
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    mail_from: str = ""
    # Public address of the app for links in mails, e.g. https://flights.example.ch.
    public_base_url: str = ""
    # AI (optional; Platform Settings → AI). Empty provider = no AI features.
    ai_provider: str = Field(default="", pattern=r"^(|ollama|openai|anthropic)$")
    ai_base_url: str = ""
    ai_model: str = ""
    ai_api_key: SecretStr | None = None
    # Upper limit of a request (prompt) and of an answer, in tokens.
    ai_max_input_tokens: int = Field(default=16000, ge=1000, le=2_000_000)
    ai_max_output_tokens: int = Field(default=8000, ge=256, le=128_000)
    # Ollama only: the context window (num_ctx) and keep-alive sent with every request – set
    # them like the other clients of the same Ollama, or it reloads/unloads the model.
    ai_ollama_num_ctx: int = Field(default=0, ge=0, le=1_048_576)  # 0: Ollama's default
    ai_ollama_keep_alive: str = Field(default="", pattern=r"^(|-1|\d+(\.\d+)?(ms|s|m|h)?)$")
    # Ollama only: ask thinking models (qwen3, deepseek-r1, …) not to think (`think: false`).
    ai_ollama_no_thinking: bool = False
    # Empty: the built-in template (services/ai_prompts.py).
    ai_system_prompt: str = ""
    ai_drafts: bool = True
    ai_explanations: bool = True
    # After a notable poll the worker asks for a few sentences (services/poll_reports.py).
    ai_poll_summaries: bool = True
    # The AI's overview on top of a price digest mail (services/digest.py).
    ai_digest_overview: bool = True
    # Usage log: also keep the messages (prompts, thinking, answers) of each request, 30 days.
    ai_log_messages: bool = True
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
