from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flighttracker.domain.locations import LocationRef
from flighttracker.models.base import Base, str_enum


class SearchStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class LocationRole(StrEnum):
    ORIGIN = "origin"
    DESTINATION = "destination"


class Search(Base):
    """A Suchabo. Soft-deleted via `archived_at`; hard delete cascades to all its history."""

    __tablename__ = "searches"
    __table_args__ = (CheckConstraint("poll_interval_minutes >= 15", name="poll_interval_min"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[SearchStatus] = mapped_column(
        str_enum(SearchStatus, "search_status"), default=SearchStatus.ACTIVE
    )
    filters: Mapped[dict] = mapped_column(JSONB)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    locations: Mapped[list["SearchLocation"]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SearchLocation.id",
    )
    # Read-only: revisions are only ever inserted explicitly by the searches service.
    revisions: Mapped[list["SearchRevision"]] = relationship(
        order_by="SearchRevision.revision_no.desc()",
        viewonly=True,
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class SearchLocation(Base):
    """One origin or destination of a Suchabo: exactly one of airport or country is set.

    Country rows list the airports included in the queries (`airport_codes`).
    """

    __tablename__ = "search_locations"
    __table_args__ = (
        CheckConstraint(
            "(airport_code IS NULL) <> (country_code IS NULL)", name="exactly_one_target"
        ),
        CheckConstraint(
            "(country_code IS NULL) = (airport_codes IS NULL)", name="airports_for_country"
        ),
        UniqueConstraint(
            "search_id",
            "role",
            "airport_code",
            "country_code",
            name="uq_search_locations_target",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[LocationRole] = mapped_column(str_enum(LocationRole, "location_role"))
    airport_code: Mapped[str | None] = mapped_column(ForeignKey("airports.iata_code"))
    country_code: Mapped[str | None] = mapped_column(ForeignKey("countries.code"))
    airport_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String(3)))

    search: Mapped[Search] = relationship(back_populates="locations")

    @property
    def ref(self) -> LocationRef:
        if self.airport_code is not None:
            return LocationRef.airport(self.airport_code)
        return LocationRef.country(self.country_code)


class SearchRevision(Base):
    """Immutable snapshot of a Suchabo after each change (UPDATE blocked by DB trigger)."""

    __tablename__ = "search_revisions"
    __table_args__ = (
        UniqueConstraint("search_id", "revision_no", name="uq_search_revisions_search_revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
