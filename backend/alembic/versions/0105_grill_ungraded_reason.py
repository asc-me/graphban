"""Persist why a grill round could not be graded (GRPH-485, read path).

`classify_grill` has returned `graded` / `ungraded_reason` since GRPH-485 and
`answer_grill` relays them, but the fact lived only in that one response. Every later
read — `grill_state`, and so the whole PRD editor — saw the previous round's outcomes
with nothing to say they were the previous round's. An author answering into a dead
grader was told `outstanding`, same as an author whose answer was too thin.

One column, empty when the last attempt produced a verdict.

Revision ID: 0105
Revises: 0104
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0105"
down_revision: Union[str, None] = "0104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prds",
        sa.Column("grill_ungraded_reason", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("prds", "grill_ungraded_reason")
