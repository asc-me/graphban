"""Per-user UI preferences: one document per user, `user_id` as the key (PRD-60 D1 /
GRPH-1133).

No surrogate id and no UNIQUE beside it — a user has at most one document, and making that
the primary key is what lets the absence of a row mean "never written" rather than "empty".
The client's localStorage migration reads that distinction: it uploads once only when the
server has nothing.

Revision ID: 0144
Revises: 0143
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0144"
down_revision: Union[str, None] = "0143"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("doc", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
