"""`memory_shards.retired_at`: a retired shard is recoverable for 14 days (GRPH-1175).

Revision ID: 0151
Revises: 0150
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0151"
down_revision: Union[str, None] = "0150"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_shards", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("memory_shards", "retired_at")
