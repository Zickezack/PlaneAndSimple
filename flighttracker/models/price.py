from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.domain.filters import CabinClass
from flighttracker.models.base import Base, str_enum


class PriceSource(StrEnum):
    LIVE = "live"
    # Written by the former initial backfill (removed); kept so that older rows stay valid.
    BACKFILL = "backfill"


class PriceHistory(Base):
    """One observed price. Append-only (UPDATE blocked by DB trigger).

    Every row carries its own concrete route/date/cabin, so it stays meaningful
    no matter how the Suchabo's filters change later.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        UniqueConstraint(
            "search_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
            "stops",
            "provider",
            "source",
            "observed_at",
            name="uq_price_history_observation",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_price_history_search_observed", "search_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    search_revision_id: Mapped[int] = mapped_column(
        ForeignKey("search_revisions.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[PriceSource] = mapped_column(str_enum(PriceSource, "price_source"))
    provider: Mapped[str] = mapped_column(String(40))
    origin_iata: Mapped[str] = mapped_column(String(3))
    destination_iata: Mapped[str] = mapped_column(String(3))
    departure_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    cabin_class: Mapped[CabinClass] = mapped_column(str_enum(CabinClass, "cabin_class"))
    stops: Mapped[int | None] = mapped_column(SmallInteger)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))
    airline: Mapped[str | None] = mapped_column(String(60))
    # Flight details from the provider (flights, times, duration, link) – see datamodel.md.
    details: Mapped[dict | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
