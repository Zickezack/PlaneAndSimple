"""Logs for admins: every mail sent (or not), every request to the AI provider."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.models.base import Base


class MailLog(Base):
    """One mail: what kind, to whom (user id – the address itself is not logged), outcome."""

    __tablename__ = "mail_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # welcome, reset, password_changed, price_alert, test
    kind: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(200))
    ok: Mapped[bool]
    error: Mapped[str | None] = mapped_column(Text)


class AIUsage(Base):
    """One request to the AI provider: feature, model, tokens (when the provider reports
    them), duration and outcome. No prompts or answers are stored."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # draft, explain, test
    feature: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(120))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool]
    error: Mapped[str | None] = mapped_column(Text)
