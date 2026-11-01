"""`memory_shards.shard_class`: pitfall / convention / principle, "" = unclassified (GRPH-1181).

Revision ID: 0154
Revises: 0153
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0154"
down_revision: Union[str, None] = "0153"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_shards",
        sa.Column("shard_class", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("memory_shards", "shard_class")
