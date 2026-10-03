"""Airport selection for country locations, airport size ranking, backfill jobs removed.

Existing country locations get the airports they were expanded to so far (scheduled
large/medium airports), so their queries stay the same.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("airports", sa.Column("passengers", sa.BigInteger()))
    op.add_column("search_locations", sa.Column("airport_codes", postgresql.ARRAY(sa.String(3))))
    op.execute(
        """
        UPDATE search_locations AS loc
           SET airport_codes = coalesce(
               (SELECT array_agg(a.iata_code ORDER BY a.iata_code)
                  FROM airports a
                 WHERE a.country_code = loc.country_code
                   AND a.has_scheduled_service
                   AND a.airport_type IN ('large_airport', 'medium_airport')),
               '{}')
         WHERE loc.country_code IS NOT NULL
        """
    )
    op.create_check_constraint(
        "airports_for_country",
        "search_locations",
        "(country_code IS NULL) = (airport_codes IS NULL)",
    )

    # Backfill jobs are operational data only; the prices they stored are kept.
    op.execute("DELETE FROM fetch_jobs WHERE kind = 'backfill'")
    op.drop_constraint("job_kind", "fetch_jobs", type_="check")
    op.create_check_constraint("job_kind", "fetch_jobs", "kind IN ('poll')")


def downgrade() -> None:
    op.drop_constraint("job_kind", "fetch_jobs", type_="check")
    op.create_check_constraint("job_kind", "fetch_jobs", "kind IN ('backfill', 'poll')")
    op.drop_constraint("airports_for_country", "search_locations", type_="check")
    op.drop_column("search_locations", "airport_codes")
    op.drop_column("airports", "passengers")
