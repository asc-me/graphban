"""Org-scoped handles, handle holds and the user dot (GRPH-1787, P93 S1).

`org_memberships.handle` is the handle a user goes by inside one org, unique per
(org_id, handle) and backfilled from `users.handle` (the only row rewrite). The
global unique on `users.handle` is dropped on Postgres; the self-hosted box keeps
account handles unique in the service. `handle_holds` reserves a released handle
for 30 days. `users.avatar2` / `users.avatar_pattern` mirror the project tile fill.

Revision ID: 0222
Revises: 0221
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0222"
down_revision: Union[str, None] = "0221"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column("org_memberships", sa.Column("handle", sa.String(), nullable=True))
    op.execute(
        "UPDATE org_memberships SET handle = "
        "(SELECT users.handle FROM users WHERE users.id = org_memberships.user_id)"
    )
    with op.batch_alter_table("org_memberships") as batch:
        batch.alter_column("handle", existing_type=sa.String(), nullable=False)
        batch.create_unique_constraint("uq_org_membership_handle", ["org_id", "handle"])

    op.add_column("users", sa.Column("avatar2", sa.String(), nullable=True))
    op.add_column("users", sa.Column(
        "avatar_pattern", sa.String(), nullable=False, server_default="none"))
    if _pg():
        # SQLite never runs migrations in production (create_all owns it) and an
        # unnamed column UNIQUE there cannot be dropped by name.
        op.drop_constraint("users_handle_key", "users", type_="unique")

    op.create_table(
        "handle_holds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_handle_holds_org_handle", "handle_holds", ["org_id", "handle"])


def downgrade() -> None:
    op.drop_index("ix_handle_holds_org_handle", table_name="handle_holds")
    op.drop_table("handle_holds")
    if _pg():
        op.create_unique_constraint("users_handle_key", "users", ["handle"])
    op.drop_column("users", "avatar_pattern")
    op.drop_column("users", "avatar2")
    with op.batch_alter_table("org_memberships") as batch:
        batch.drop_constraint("uq_org_membership_handle", type_="unique")
        batch.drop_column("handle")
