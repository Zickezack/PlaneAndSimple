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
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flighttracker.models.base import Base, str_enum
from flighttracker.models.user import User


class TokenPurpose(StrEnum):
    RESET = "reset"  # "Forgot password?"
    WELCOME = "welcome"  # new user sets the first password


class PasswordToken(Base):
    """One-time link to set a password. Only the SHA-256 of the token is stored."""

    __tablename__ = "password_tokens"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[TokenPurpose] = mapped_column(str_enum(TokenPurpose, "token_purpose"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()


class PriceAlert(Base):
    """A user wants a mail when a flight of a Suchabo costs at most `max_price` – optionally
    only for one route and/or cabin."""

    __tablename__ = "price_alerts"
    __table_args__ = (CheckConstraint("max_price > 0", name="max_price_positive"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    max_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))
    origin_iata: Mapped[str | None] = mapped_column(String(3))
    destination_iata: Mapped[str | None] = mapped_column(String(3))
    cabin_class: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()


class PriceAlertNotification(Base):
    """A flight already reported by an alert, at this price: reported again only cheaper."""

    __tablename__ = "price_alert_notifications"
    __table_args__ = (
        Index(
            "ix_price_alert_notifications_flight",
            "alert_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("price_alerts.id", ondelete="CASCADE"))
    origin_iata: Mapped[str] = mapped_column(String(3))
    destination_iata: Mapped[str] = mapped_column(String(3))
    cabin_class: Mapped[str] = mapped_column(String(20))
    departure_date: Mapped[date] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
