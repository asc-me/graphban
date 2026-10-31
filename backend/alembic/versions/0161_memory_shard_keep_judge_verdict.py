"""`memory_shards.keep_judge_verdict`: the stored keep/quality verdict (GRPH-1242).

Revision ID: 0161
Revises: 0160
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0161"
down_revision: Union[str, None] = "0160"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memory_shards", sa.Column("keep_judge_verdict", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_shards", "keep_judge_verdict")
