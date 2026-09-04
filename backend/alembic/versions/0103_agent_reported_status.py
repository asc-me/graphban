"""What an agent says it is doing (PRD-34 D5).

`heartbeat` gains `status` (one line) and `files` (paths being edited). They live on
the agent row, last-write-wins; the history is the `reported` rows in `agent_calls`,
written only when the report changed. Self-reported, like `worktree` and `branch`,
and rendered with its age and a stale mark — never as an observation.

Revision ID: 0103
Revises: 0102
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0103"
down_revision: Union[str, None] = "0102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("status_text", sa.String(length=200), nullable=False,
                                      server_default=""))
    op.add_column("agents", sa.Column("status_files", sa.JSON(), nullable=True))
    op.add_column("agents", sa.Column("status_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "status_at")
    op.drop_column("agents", "status_files")
    op.drop_column("agents", "status_text")
