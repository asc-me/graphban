"""When a supervisor put an attempt's branch where a reviewer can read it (GRPH-754).

Measured on the deployed instance: an item became reviewable at 12:07:31 and its branch was
pushed at reap, after the child exited, some seconds later. A reviewer that fetched inside that
window correctly reported a 404 for a branch that now exists — and bounced work that was fine.

The handoff is not complete when the item says `review`; it is complete when the branch is
reachable. This records the second event so the server can stop handing out the first.

Revision ID: 0114
Revises: 0113
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0114"
down_revision: Union[str, None] = "0113"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("attempt_telemetry",
                  sa.Column("branch_published_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("attempt_telemetry", "branch_published_at")
