"""`memory_shards.cited_code_hashes`: the cited code's content hashes at publish/Keep (GRPH-1213).

Revision ID: 0157
Revises: 0156
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0157"
down_revision: Union[str, None] = "0156"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memory_shards", sa.Column("cited_code_hashes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_shards", "cited_code_hashes")
