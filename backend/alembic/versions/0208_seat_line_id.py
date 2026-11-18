"""seat_line_id: `enrolments.line_id`, the line a seat is scoped to (GRPH-1685).

Revision ID: 0208
Revises: 0207
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0208"
down_revision: Union[str, None] = "0207"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrolments", sa.Column("line_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("enrolments", "line_id")
