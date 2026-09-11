"""How many times an item was handed back by a worker (GRPH-783).

Measured on the live instance on 2026-09-07: with the review queue empty, `claim_cluster`
returned two clusters back to back to one worker seat, every member `ready: false` — one
waiting on an unfinished dependency, two whose only prescribed next step was a grill that a
worker seat is forbidden to run. Releasing the first cluster handed two of its three items
straight back in the second. One of them had by then been released three times, and nothing
anywhere read that.

The count is the fact every hand-back leaves behind. At `items.RELEASE_HOLD` it parks the item
out of the claim pool — reported as withheld, never silently dropped — until a planner
delegates it, which is the touch that resets it.

Revision ID: 0124
Revises: 0123
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0124"
down_revision: Union[str, None] = "0123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("releases", sa.Integer(), nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    op.drop_column("items", "releases")
