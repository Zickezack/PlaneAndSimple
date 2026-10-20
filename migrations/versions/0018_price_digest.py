"""Price digest: how often a user gets it, and when it last ran.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("digest", sa.String(20), server_default="off", nullable=False))
    op.create_check_constraint("digest_frequency", "users", "digest IN ('off', 'daily', 'weekly')")
    op.add_column("users", sa.Column("digest_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("users", "digest_at")
    op.drop_constraint("digest_frequency", "users", type_="check")
    op.drop_column("users", "digest")
