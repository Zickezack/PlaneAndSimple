"""Logs for admins: every mail sent (or not), every request to the AI provider."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
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
    them), duration and outcome; the messages only while logging them is on, for 30 days
    (services/ai_log.py)."""

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
    # Tokens the answer could use, and whether it hit that limit (then ok is false).
    output_limit: Mapped[int | None] = mapped_column(Integer)
    cut_off: Mapped[bool] = mapped_column(default=False, server_default="false")
    stop_reason: Mapped[str | None] = mapped_column(String(40))  # as the provider says it
    # Address and parameters sent (no headers, no prompts); the provider's response without
    # the texts (timings, ids, token details).
    request: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    # The messages: cleared after 30 days, never stored while logging them is off.
    system_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt: Mapped[str | None] = mapped_column(Text)
    thinking: Mapped[str | None] = mapped_column(Text)  # returned or written into the answer
    answer: Mapped[str | None] = mapped_column(Text)  # as received
    result: Mapped[str | None] = mapped_column(Text)  # as the app used it
