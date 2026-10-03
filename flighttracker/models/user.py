from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flighttracker.models.base import Base, str_enum

if TYPE_CHECKING:
    from flighttracker.models.search import Search
    from flighttracker.models.trip import Trip


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    """A login. Personal data (username, password hash, preferences) – see datamodel.md.

    The admin from `.env` (`ADMIN_USERNAME`) is a row too, created on its first login, but
    without `password_hash`: its password is always checked against `ADMIN_PASSWORD_HASH`.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_username_lower", text("lower(username)"), unique=True),
        CheckConstraint("max_searches IS NULL OR max_searches >= 0", name="max_searches"),
        CheckConstraint("max_requests IS NULL OR max_requests >= 0", name="max_requests"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(60))
    password_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole, "user_role"), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-user quota overrides; None = the platform defaults (Platform Settings → Users).
    max_searches: Mapped[int | None] = mapped_column(Integer)
    max_requests: Mapped[int | None] = mapped_column(Integer)
    # Personal display settings; None = the platform default.
    locale: Mapped[str | None] = mapped_column(String(5))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Raised on password, role or status changes; sessions carrying an older value end.
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class Share(Base):
    """Access to one Suchabo or one Trip for another user: view only, or view and edit."""

    __tablename__ = "shares"
    __table_args__ = (
        CheckConstraint("(search_id IS NULL) <> (trip_id IS NULL)", name="exactly_one_target"),
        UniqueConstraint(
            "search_id",
            "trip_id",
            "user_id",
            name="uq_shares_target_user",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int | None] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    trip_id: Mapped[int | None] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()
    search: Mapped["Search | None"] = relationship()
    trip: Mapped["Trip | None"] = relationship()
