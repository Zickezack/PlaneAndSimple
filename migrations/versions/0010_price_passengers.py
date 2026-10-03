"""Passengers per observed price.

A price for two adults is not comparable with one for a single adult, so every price row now
records the passengers it was requested for. Existing rows take them from the revision they
were recorded under (immutable as well); the append-only trigger is lifted for this one
backfill only.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("price_history", sa.Column("adults", sa.SmallInteger()))
    op.add_column("price_history", sa.Column("children", sa.SmallInteger()))
    op.execute("ALTER TABLE price_history DISABLE TRIGGER trg_price_history_append_only")
    # Snapshots of normal revisions hold the filters under "filters"; imported Suchabos got a
    # revision whose snapshot *is* the filters (see services/data_transfer.py).
    op.execute(
        """
        UPDATE price_history p
           SET adults = COALESCE(
                   (r.snapshot -> 'filters' ->> 'adults')::smallint,
                   (r.snapshot ->> 'adults')::smallint, 1),
               children = COALESCE(
                   (r.snapshot -> 'filters' ->> 'children')::smallint,
                   (r.snapshot ->> 'children')::smallint, 0)
          FROM search_revisions r
         WHERE r.id = p.search_revision_id
        """
    )
    op.execute("ALTER TABLE price_history ENABLE TRIGGER trg_price_history_append_only")
    op.alter_column("price_history", "adults", nullable=False)
    op.alter_column("price_history", "children", nullable=False)


def downgrade() -> None:
    op.drop_column("price_history", "children")
    op.drop_column("price_history", "adults")
