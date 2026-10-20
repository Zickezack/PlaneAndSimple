"""What each completed poll changed (domain/poll_report.py, domain/trip_report.py), stored by
the worker right after the poll – with the AI's summary of a notable one."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.models.base import Base


class PollReport(Base):
    """One per completed poll of a Suchabo or of a Trip; pruned after 90 days."""

    __tablename__ = "poll_reports"
    __table_args__ = (
        CheckConstraint("(search_id IS NULL) <> (trip_id IS NULL)", name="one_target"),
        Index("ix_poll_reports_search", "search_id", "created_at"),
        Index("ix_poll_reports_trip", "trip_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("fetch_jobs.id", ondelete="CASCADE"), unique=True
    )
    search_id: Mapped[int | None] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    facts: Mapped[dict] = mapped_column(JSONB)
    notable: Mapped[bool] = mapped_column(Boolean)
    # The AI's words for a notable poll, in the owner's language (None: not asked or failed).
    summary: Mapped[str | None] = mapped_column(Text)
    summary_locale: Mapped[str | None] = mapped_column(String(5))


class DealNotification(Base):
    """An unusually cheap flight already mailed to a user, at this price: mailed again only
    cheaper (services/deal_radar.py)."""

    __tablename__ = "deal_notifications"
    __table_args__ = (
        Index(
            "ix_deal_notifications_flight",
            "search_id",
            "user_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    origin_iata: Mapped[str] = mapped_column(String(3))
    destination_iata: Mapped[str] = mapped_column(String(3))
    cabin_class: Mapped[str] = mapped_column(String(20))
    departure_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
