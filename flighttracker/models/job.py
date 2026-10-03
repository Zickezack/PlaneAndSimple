from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.models.base import Base, str_enum


class JobKind(StrEnum):
    POLL = "poll"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class FetchJob(Base):
    """Work queue for the worker, claimed with SELECT … FOR UPDATE SKIP LOCKED."""

    __tablename__ = "fetch_jobs"
    __table_args__ = (Index("ix_fetch_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    trip_id: Mapped[int | None] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind, "job_kind"))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    quotes_stored: Mapped[int] = mapped_column(Integer, default=0)
    queries_total: Mapped[int] = mapped_column(Integer, default=0)
    queries_failed: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
