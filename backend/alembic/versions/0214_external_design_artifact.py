"""External design artifact on a design set (GRPH-1692).

A link plus page names, per section, for a design that was never compiled into
screens. Nullable: a set with no external artifact stays null, which is "none
registered", not a missing column.

Revision ID: 0214
Revises: 0213
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0214"
down_revision: Union[str, None] = "0213"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("design_sets", sa.Column("external_artifact", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("design_sets", "external_artifact")
