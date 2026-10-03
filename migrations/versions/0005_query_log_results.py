"""What each logged query found (departure dates and prices).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("query_log", sa.Column("results", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("query_log", "results")
