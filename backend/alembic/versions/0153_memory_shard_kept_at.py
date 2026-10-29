"""`memory_shards.kept_at`: a person confirmed a stale shard is still right (GRPH-1180).

Revision ID: 0153
Revises: 0152
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0153"
down_revision: Union[str, None] = "0152"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_shards", sa.Column("kept_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("memory_shards", "kept_at")
