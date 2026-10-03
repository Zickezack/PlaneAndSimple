"""User management: users, owners of Suchabos and Trips, shares, cancellable polls.

Existing Suchabos and Trips keep `owner_id` NULL, which only admins can see; an admin can
assign them to a user afterwards. The admin from `.env` gets its row on its first login.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("username", sa.String(60), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("role", _enum("user_role", "admin", "user"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("max_searches", sa.Integer()),
        sa.Column("max_requests", sa.Integer()),
        sa.Column("locale", sa.String(5)),
        sa.Column("timezone", sa.String(64)),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("max_searches IS NULL OR max_searches >= 0", name="max_searches"),
        sa.CheckConstraint("max_requests IS NULL OR max_requests >= 0", name="max_requests"),
    )
    op.create_index("uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True)

    for table in ("searches", "trips"):
        op.add_column(
            table,
            sa.Column("owner_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        )
        op.create_index(op.f(f"ix_{table}_owner_id"), table, ["owner_id"])

    op.create_table(
        "shares",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("search_id", sa.BigInteger(), sa.ForeignKey("searches.id", ondelete="CASCADE")),
        sa.Column("trip_id", sa.BigInteger(), sa.ForeignKey("trips.id", ondelete="CASCADE")),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("can_edit", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("(search_id IS NULL) <> (trip_id IS NULL)", name="exactly_one_target"),
        sa.UniqueConstraint(
            "search_id",
            "trip_id",
            "user_id",
            name="uq_shares_target_user",
            postgresql_nulls_not_distinct=True,
        ),
    )
    for column in ("search_id", "trip_id", "user_id"):
        op.create_index(op.f(f"ix_shares_{column}"), "shares", [column])

    op.drop_constraint("job_status", "fetch_jobs", type_="check")
    op.create_check_constraint(
        "job_status", "fetch_jobs", "status IN ('queued', 'running', 'done', 'failed', 'cancelled')"
    )


def downgrade() -> None:
    op.execute("UPDATE fetch_jobs SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("job_status", "fetch_jobs", type_="check")
    op.create_check_constraint(
        "job_status", "fetch_jobs", "status IN ('queued', 'running', 'done', 'failed')"
    )
    op.drop_table("shares")
    for table in ("searches", "trips"):
        op.drop_index(op.f(f"ix_{table}_owner_id"), table_name=table)
        op.drop_column(table, "owner_id")
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_table("users")
