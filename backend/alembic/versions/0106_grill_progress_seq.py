"""Track where a grill last moved, so a grill that has stopped moving can say so.

Answers past this mark are answers that changed no dimension. Stamped on an outcome
CHANGE rather than derived from `grill_dimensions.turn_seq`, because a deferral is
progress and cites no turn — `turn_seq` is a provenance pointer at the answer a verdict
cites, and overloading it with "when did this change" would corrupt the thing baselines
read.

Revision ID: 0106
Revises: 0105
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0106"
down_revision: Union[str, None] = "0105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prds",
        sa.Column("grill_progress_seq", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("prds", "grill_progress_seq")
