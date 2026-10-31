"""Store the keep/quality verdict behind Stale-or-wrong recommendations (GRPH-1242).

GET /api/memory/stale asked the decider once per row, one after another, on every load
(about 0.5s each). The verdict is now stored on the shard, keyed on a digest of the text it
judged, and the page reads it; judging happens only on an explicit refresh.

Revision ID: 0161
Revises: 0160
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0161"
down_revision: Union[str, None] = "0160"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memory_shards", sa.Column("keep_judge_key", sa.String(),
                                             nullable=False, server_default=""))
    op.add_column("memory_shards", sa.Column("keep_judge_verdict", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_shards", "keep_judge_verdict")
    op.drop_column("memory_shards", "keep_judge_key")
