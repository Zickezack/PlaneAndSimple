"""Poll schedule: planned start of each Suchabo's and Trip's poll within the poll interval.

`NULL` until the worker places it (new, imported and existing records alike).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("searches", "trips"):
        op.add_column(table, sa.Column("poll_offset_minutes", sa.Integer()))
        op.create_check_constraint("poll_offset_non_negative", table, "poll_offset_minutes >= 0")


def downgrade() -> None:
    for table in ("searches", "trips"):
        op.drop_constraint("poll_offset_non_negative", table, type_="check")
        op.drop_column(table, "poll_offset_minutes")
