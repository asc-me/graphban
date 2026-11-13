"""vapid_keys: server-generated VAPID key pair for browser push (GRPH-1582).

Revision ID: 0200
Revises: 0199
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0200"
down_revision: Union[str, None] = "0199"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vapid_keys",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("private_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vapid_keys")
