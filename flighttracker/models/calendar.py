from datetime import date, datetime
from decimal import Decimal

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
)
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.domain.filters import CabinClass
from flighttracker.models.base import Base, str_enum


class CalendarObservation(Base):
    """The cheapest price of one departure day from a provider's price calendar. Append-only.

    Separate from `price_history`: a calendar price has no flight behind it (no stops, airline
    or times), so it feeds the heat map only – never the chart, table or overlap rules.
    """

    __tablename__ = "price_calendar"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        UniqueConstraint(
            "search_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
            "provider",
            "observed_at",
            name="uq_price_calendar_observation",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_price_calendar_search_observed", "search_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40))
    origin_iata: Mapped[str] = mapped_column(String(3))
    destination_iata: Mapped[str] = mapped_column(String(3))
    cabin_class: Mapped[CabinClass] = mapped_column(str_enum(CabinClass, "cabin_class"))
    departure_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))
    adults: Mapped[int] = mapped_column(SmallInteger)
    children: Mapped[int] = mapped_column(SmallInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
