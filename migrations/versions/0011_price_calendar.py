"""Price calendar for the heat map, and the kind of each logged provider query.

`price_calendar` holds the cheapest price per departure day from a provider's calendar view
(append-only, like the price history). `query_log.kind` tells calendar requests apart from
the fare queries a resumed poll may skip.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.create_table(
        "price_calendar",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("origin_iata", sa.String(3), nullable=False),
        sa.Column("destination_iata", sa.String(3), nullable=False),
        sa.Column(
            "cabin_class",
            _enum("cabin_class", "economy", "premium_economy", "business", "first"),
            nullable=False,
        ),
        sa.Column("departure_date", sa.Date(), nullable=False),
        sa.Column("return_date", sa.Date()),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("adults", sa.SmallInteger(), nullable=False),
        sa.Column("children", sa.SmallInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price >= 0", name="price_non_negative"),
        sa.UniqueConstraint(
            "search_id",
            "origin_iata",
            "destination_iata",
            "cabin_class",
            "departure_date",
            "return_date",
            "provider",
            "observed_at",
            name="uq_price_calendar_observation",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_price_calendar_search_observed", "price_calendar", ["search_id", "observed_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_price_calendar_append_only BEFORE UPDATE ON price_calendar "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update()"
    )
    op.add_column(
        "query_log",
        sa.Column(
            "kind", _enum("query_kind", "fares", "calendar"), nullable=False, server_default="fares"
        ),
    )


def downgrade() -> None:
    op.drop_column("query_log", "kind")
    op.execute("DROP TRIGGER IF EXISTS trg_price_calendar_append_only ON price_calendar")
    op.drop_table("price_calendar")
