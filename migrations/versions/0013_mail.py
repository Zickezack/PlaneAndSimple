"""Mail: optional e-mail address per user, one-time password links, price alerts.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(254)))
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "password_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "reset",
                "welcome",
                name="token_purpose",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_password_tokens_user_id", "password_tokens", ["user_id"])
    op.create_table(
        "price_alerts",
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
        sa.Column("max_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("origin_iata", sa.String(3)),
        sa.Column("destination_iata", sa.String(3)),
        sa.Column("cabin_class", sa.String(20)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("max_price > 0", name="max_price_positive"),
    )
    op.create_index("ix_price_alerts_search_id", "price_alerts", ["search_id"])
    op.create_index("ix_price_alerts_user_id", "price_alerts", ["user_id"])
    op.create_table(
        "price_alert_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "alert_id",
            sa.BigInteger(),
            sa.ForeignKey("price_alerts.id", ondelete="CASCADE"),
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
    op.create_index(
        "ix_price_alert_notifications_flight",
        "price_alert_notifications",
        [
            "alert_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
        ],
    )


def downgrade() -> None:
    op.drop_table("price_alert_notifications")
    op.drop_table("price_alerts")
    op.drop_table("password_tokens")
    op.drop_index("uq_users_email_lower", "users")
    op.drop_column("users", "email")
