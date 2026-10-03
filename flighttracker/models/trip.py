from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flighttracker.models.base import Base, str_enum
from flighttracker.models.search import SearchStatus

if TYPE_CHECKING:
    from flighttracker.models.search import Search


class Trip(Base):
    """A bounded itinerary made from ordinary, separately tracked searches."""

    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="date_window"),
        CheckConstraint("poll_interval_minutes >= 15", name="poll_interval_min"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[SearchStatus] = mapped_column(
        str_enum(SearchStatus, "trip_status"), default=SearchStatus.ACTIVE
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Same meaning as `Search.owner_id`; the Trip's leg Suchabos have the same owner.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    legs: Mapped[list["TripLeg"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="TripLeg.position"
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class TripLeg(Base):
    """Ordered reference to a normal Suchabo; wait bounds apply before this leg."""

    __tablename__ = "trip_legs"
    __table_args__ = (
        UniqueConstraint("trip_id", "position", name="uq_trip_legs_trip_position"),
        UniqueConstraint("search_id", name="uq_trip_legs_search"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "min_layover_days IS NULL OR max_layover_days IS NULL "
            "OR max_layover_days >= min_layover_days",
            name="layover_bounds",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    min_layover_days: Mapped[int | None] = mapped_column(Integer)
    max_layover_days: Mapped[int | None] = mapped_column(Integer)

    trip: Mapped[Trip] = relationship(back_populates="legs")
    search: Mapped["Search"] = relationship()
