"""Poll reports: what every completed poll of a Suchabo or Trip changed, with the AI's summary.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "poll_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("fetch_jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("search_id", sa.BigInteger(), sa.ForeignKey("searches.id", ondelete="CASCADE")),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("facts", JSONB(), nullable=False),
        sa.Column("notable", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_locale", sa.String(5)),
        sa.CheckConstraint("(search_id IS NULL) <> (trip_id IS NULL)", name="one_target"),
    )
    op.create_index("ix_poll_reports_search", "poll_reports", ["search_id", "created_at"])
    op.create_index("ix_poll_reports_trip", "poll_reports", ["trip_id", "created_at"])


def downgrade() -> None:
    op.drop_table("poll_reports")
