"""Deal radar: unusually cheap flights already mailed, and the users' switch for these mails.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deal_mails", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_table(
        "deal_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin_iata", sa.String(3), nullable=False),
        sa.Column("destination_iata", sa.String(3), nullable=False),
        sa.Column("cabin_class", sa.String(20), nullable=False),
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column("return_date", sa.Date()),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deal_notifications_user_id", "deal_notifications", ["user_id"])
    op.create_index(
        "ix_deal_notifications_flight",
        "deal_notifications",
        [
            "search_id",
            "user_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
        ],
    )


def downgrade() -> None:
    op.drop_table("deal_notifications")
    op.drop_column("users", "deal_mails")
