"""Global, persistent query log; query counters on fetch jobs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.add_column(
        "fetch_jobs", sa.Column("queries_total", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "fetch_jobs", sa.Column("queries_failed", sa.Integer(), nullable=False, server_default="0")
    )
    op.alter_column("fetch_jobs", "queries_total", server_default=None)
    op.alter_column("fetch_jobs", "queries_failed", server_default=None)

    # No foreign keys on purpose: log entries outlive jobs and deleted Suchabos.
    op.create_table(
        "query_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.BigInteger()),
        sa.Column("search_id", sa.BigInteger()),
        sa.Column("search_name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("origin_iata", sa.String(3), nullable=False),
        sa.Column("destination_iata", sa.String(3), nullable=False),
        sa.Column("departure_month", sa.Date(), nullable=False),
        sa.Column(
            "cabin_class",
            _enum("cabin_class", "economy", "premium_economy", "business", "first"),
            nullable=False,
        ),
        sa.Column("outcome", _enum("query_outcome", "ok", "empty", "failed"), nullable=False),
        sa.Column("quotes_found", sa.Integer(), nullable=False),
        sa.Column("quotes_stored", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "logged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_query_log_started_at", "query_log", ["started_at"])
    op.create_index("ix_query_log_search_started", "query_log", ["search_id", "started_at"])
    # Append-only like the price history (function from migration 0001).
    op.execute(
        "CREATE TRIGGER trg_query_log_append_only BEFORE UPDATE ON query_log "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_query_log_append_only ON query_log")
    op.drop_table("query_log")
    op.drop_column("fetch_jobs", "queries_failed")
    op.drop_column("fetch_jobs", "queries_total")
