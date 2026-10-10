from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Date, DateTime, Identity, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.domain.filters import CabinClass
from flighttracker.models.base import Base, str_enum


class QueryKind(StrEnum):
    FARES = "fares"  # prices with their flights for sampled days (price_history)
    CALENDAR = "calendar"  # cheapest price per day of a date range (price_calendar)


class QueryOutcome(StrEnum):
    OK = "ok"  # prices found
    EMPTY = "empty"  # the source answered, but had no (matching) flights
    FAILED = "failed"


class QueryLog(Base):
    """One provider query (route × departure month × cabin) of a poll. Append-only.

    Deliberately without foreign keys: the log is global and persistent, so it outlives
    jobs and even permanently deleted Suchabos (their name is kept as a snapshot).
    """

    __tablename__ = "query_log"
    __table_args__ = (
        Index("ix_query_log_started_at", "started_at"),
        Index("ix_query_log_search_started", "search_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    search_id: Mapped[int | None] = mapped_column(BigInteger)
    search_name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(40))
    origin_iata: Mapped[str] = mapped_column(String(3))
    destination_iata: Mapped[str] = mapped_column(String(3))
    departure_month: Mapped[date] = mapped_column(Date)
    cabin_class: Mapped[CabinClass] = mapped_column(str_enum(CabinClass, "cabin_class"))
    kind: Mapped[QueryKind] = mapped_column(
        str_enum(QueryKind, "query_kind"), default=QueryKind.FARES, server_default="fares"
    )
    outcome: Mapped[QueryOutcome] = mapped_column(str_enum(QueryOutcome, "query_outcome"))
    quotes_found: Mapped[int] = mapped_column(Integer, default=0)
    quotes_stored: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    # What the query found: [{"date", "return", "price", "stops"}] – shown when unfolding the entry.
    results: Mapped[list | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
