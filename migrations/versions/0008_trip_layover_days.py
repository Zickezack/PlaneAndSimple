"""Store Trip layovers as calendar days.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_trip_legs_layover_bounds"), "trip_legs", type_="check")
    op.add_column("trip_legs", sa.Column("min_layover_days", sa.Integer()))
    op.add_column("trip_legs", sa.Column("max_layover_days", sa.Integer()))
    op.execute(
        "UPDATE trip_legs SET min_layover_days = "
        "GREATEST(1, CEIL(min_layover_minutes / 1440.0)::integer) "
        "WHERE min_layover_minutes IS NOT NULL"
    )
    op.execute(
        "UPDATE trip_legs SET max_layover_days = "
        "GREATEST(1, CEIL(max_layover_minutes / 1440.0)::integer) "
        "WHERE max_layover_minutes IS NOT NULL"
    )
    op.drop_column("trip_legs", "min_layover_minutes")
    op.drop_column("trip_legs", "max_layover_minutes")
    op.create_check_constraint(
        op.f("ck_trip_legs_layover_bounds"),
        "trip_legs",
        "min_layover_days IS NULL OR max_layover_days IS NULL "
        "OR max_layover_days >= min_layover_days",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_trip_legs_layover_bounds"), "trip_legs", type_="check")
    op.add_column("trip_legs", sa.Column("min_layover_minutes", sa.Integer()))
    op.add_column("trip_legs", sa.Column("max_layover_minutes", sa.Integer()))
    op.execute(
        "UPDATE trip_legs SET min_layover_minutes = min_layover_days * 1440 "
        "WHERE min_layover_days IS NOT NULL"
    )
    op.execute(
        "UPDATE trip_legs SET max_layover_minutes = max_layover_days * 1440 "
        "WHERE max_layover_days IS NOT NULL"
    )
    op.drop_column("trip_legs", "min_layover_days")
    op.drop_column("trip_legs", "max_layover_days")
    op.create_check_constraint(
        op.f("ck_trip_legs_layover_bounds"),
        "trip_legs",
        "min_layover_minutes IS NULL OR max_layover_minutes IS NULL "
        "OR max_layover_minutes >= min_layover_minutes",
    )
