"""Trips group ordinary Suchabos into a bounded itinerary.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trips",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "paused",
                name="trip_status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f("ck_trips_date_window")),
        sa.CheckConstraint("poll_interval_minutes >= 15", name=op.f("ck_trips_poll_interval_min")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trips")),
    )
    op.create_table(
        "trip_legs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("trip_id", sa.BigInteger(), nullable=False),
        sa.Column("search_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("min_layover_minutes", sa.Integer()),
        sa.Column("max_layover_minutes", sa.Integer()),
        sa.CheckConstraint("position >= 0", name=op.f("ck_trip_legs_position_non_negative")),
        sa.CheckConstraint(
            "min_layover_minutes IS NULL OR max_layover_minutes IS NULL "
            "OR max_layover_minutes >= min_layover_minutes",
            name=op.f("ck_trip_legs_layover_bounds"),
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"], ["trips.id"], ondelete="CASCADE", name=op.f("fk_trip_legs_trip_id_trips")
        ),
        sa.ForeignKeyConstraint(
            ["search_id"],
            ["searches.id"],
            ondelete="CASCADE",
            name=op.f("fk_trip_legs_search_id_searches"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trip_legs")),
        sa.UniqueConstraint("trip_id", "position", name=op.f("uq_trip_legs_trip_position")),
        sa.UniqueConstraint("search_id", name=op.f("uq_trip_legs_search")),
    )
    op.create_index(op.f("ix_trip_legs_trip_id"), "trip_legs", ["trip_id"])
    op.create_index(op.f("ix_trip_legs_search_id"), "trip_legs", ["search_id"])
    op.add_column("fetch_jobs", sa.Column("trip_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_fetch_jobs_trip_id_trips"),
        "fetch_jobs",
        "trips",
        ["trip_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_fetch_jobs_trip_id"), "fetch_jobs", ["trip_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_fetch_jobs_trip_id"), table_name="fetch_jobs")
    op.drop_constraint(op.f("fk_fetch_jobs_trip_id_trips"), "fetch_jobs", type_="foreignkey")
    op.drop_column("fetch_jobs", "trip_id")
    op.drop_index(op.f("ix_trip_legs_search_id"), table_name="trip_legs")
    op.drop_index(op.f("ix_trip_legs_trip_id"), table_name="trip_legs")
    op.drop_table("trip_legs")
    op.drop_table("trips")
