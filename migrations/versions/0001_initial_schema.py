"""Initial schema: geo data, Suchabos, revisions, price history, fetch jobs.

Contains schema only – no seed data, no credentials.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def _timestamp(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


APPEND_ONLY_TABLES = ("price_history", "search_revisions")


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("code", sa.String(2), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("continent", sa.String(2)),
    )
    op.create_table(
        "airports",
        sa.Column("iata_code", sa.String(3), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("city", sa.String(100)),
        sa.Column("country_code", sa.String(2), sa.ForeignKey("countries.code"), nullable=False),
        sa.Column("airport_type", sa.String(30), nullable=False),
        sa.Column("has_scheduled_service", sa.Boolean(), nullable=False),
    )
    op.create_index(op.f("ix_airports_country_code"), "airports", ["country_code"])

    op.create_table(
        "searches",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", _enum("search_status", "active", "paused"), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        _timestamp("next_poll_at"),
        _timestamp("last_polled_at"),
        _timestamp("created_at", nullable=False, default_now=True),
        _timestamp("updated_at", nullable=False, default_now=True),
        _timestamp("archived_at"),
        sa.CheckConstraint("poll_interval_minutes >= 15", name="poll_interval_min"),
    )

    op.create_table(
        "search_locations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", _enum("location_role", "origin", "destination"), nullable=False),
        sa.Column("airport_code", sa.String(3), sa.ForeignKey("airports.iata_code")),
        sa.Column("country_code", sa.String(2), sa.ForeignKey("countries.code")),
        sa.CheckConstraint(
            "(airport_code IS NULL) <> (country_code IS NULL)", name="exactly_one_target"
        ),
        sa.UniqueConstraint(
            "search_id",
            "role",
            "airport_code",
            "country_code",
            name="uq_search_locations_target",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f("ix_search_locations_search_id"), "search_locations", ["search_id"])

    op.create_table(
        "search_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        _timestamp("created_at", nullable=False, default_now=True),
        sa.UniqueConstraint("search_id", "revision_no", name="uq_search_revisions_search_revision"),
    )

    op.create_table(
        "price_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "search_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("search_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", _enum("price_source", "live", "backfill"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("origin_iata", sa.String(3), nullable=False),
        sa.Column("destination_iata", sa.String(3), nullable=False),
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column("return_date", sa.Date()),
        sa.Column(
            "cabin_class",
            _enum("cabin_class", "economy", "premium_economy", "business", "first"),
            nullable=False,
        ),
        sa.Column("stops", sa.SmallInteger()),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("airline", sa.String(60)),
        _timestamp("observed_at", nullable=False),
        _timestamp("fetched_at", nullable=False, default_now=True),
        sa.CheckConstraint("price >= 0", name="price_non_negative"),
        sa.UniqueConstraint(
            "search_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
            "stops",
            "provider",
            "source",
            "observed_at",
            name="uq_price_history_observation",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_price_history_search_observed", "price_history", ["search_id", "observed_at"]
    )
    op.create_index(
        op.f("ix_price_history_search_revision_id"), "price_history", ["search_revision_id"]
    )

    op.create_table(
        "fetch_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", _enum("job_kind", "backfill", "poll"), nullable=False),
        sa.Column(
            "status", _enum("job_status", "queued", "running", "done", "failed"), nullable=False
        ),
        _timestamp("run_after", nullable=False, default_now=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("quotes_stored", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        _timestamp("created_at", nullable=False, default_now=True),
        _timestamp("started_at"),
        _timestamp("finished_at"),
    )
    op.create_index(op.f("ix_fetch_jobs_search_id"), "fetch_jobs", ["search_id"])
    op.create_index("ix_fetch_jobs_status_run_after", "fetch_jobs", ["status", "run_after"])

    # History is append-only: rows may be inserted or deleted (cascade on hard delete),
    # but never modified – so changing a Suchabo can never rewrite past observations.
    op.execute(
        """
        CREATE FUNCTION forbid_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only; UPDATE is not allowed', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_update()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_update()")
    for table in (
        "fetch_jobs",
        "price_history",
        "search_revisions",
        "search_locations",
        "searches",
        "airports",
        "countries",
    ):
        op.drop_table(table)
