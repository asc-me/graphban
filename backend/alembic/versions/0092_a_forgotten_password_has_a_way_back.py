"""A forgotten password has a way back (GRPH-359).

Hand-written for the reason 0077-0080 were: `--autogenerate` sweeps in unrelated drift
between the models and the chain.

One table, no changes to `users`. The reset is a separate row rather than columns on the
user because it is a THING THAT EXPIRES: an account has zero or many outstanding resets over
its life, and modelling that as `reset_token`/`reset_expires_at` on the user means the second
request silently invalidates the first with nowhere to record that it happened.

`token_hash` is unique — a collision must be a database error, not an ambiguous lookup that
resets whichever row came back first.

Revision ID: 0092
Revises: 0091
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0092"
down_revision: Union[str, None] = "0091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])
    op.create_index("ix_password_resets_token_hash", "password_resets", ["token_hash"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("ix_password_resets_token_hash", table_name="password_resets")
    op.drop_index("ix_password_resets_user_id", table_name="password_resets")
    op.drop_table("password_resets")
