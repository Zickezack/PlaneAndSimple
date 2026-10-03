"""Flight details (flights, times, duration, link) on price observations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: existing (append-only) rows simply have no details.
    op.add_column("price_history", sa.Column("details", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("price_history", "details")
