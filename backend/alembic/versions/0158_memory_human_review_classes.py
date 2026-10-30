"""`projects.memory_human_review_classes`: shard classes kept out of auto-publish (GRPH-1214).

Revision ID: 0158
Revises: 0157
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0158"
down_revision: Union[str, None] = "0157"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("memory_human_review_classes", sa.JSON(), nullable=False, server_default='["principle"]'),
    )


def downgrade() -> None:
    op.drop_column("projects", "memory_human_review_classes")
