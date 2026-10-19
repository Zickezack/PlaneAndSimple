"""AI usage log: cut-off answers, the request's parameters and the messages of each request.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEXTS = ("system_prompt", "user_prompt", "thinking", "answer", "result")


def upgrade() -> None:
    op.add_column("ai_usage", sa.Column("output_limit", sa.Integer()))
    op.add_column(
        "ai_usage",
        sa.Column("cut_off", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("ai_usage", sa.Column("stop_reason", sa.String(40)))
    op.add_column("ai_usage", sa.Column("request", JSONB()))
    op.add_column("ai_usage", sa.Column("response", JSONB()))
    for name in TEXTS:
        op.add_column("ai_usage", sa.Column(name, sa.Text()))


def downgrade() -> None:
    for name in (*TEXTS, "response", "request", "stop_reason", "cut_off", "output_limit"):
        op.drop_column("ai_usage", name)
