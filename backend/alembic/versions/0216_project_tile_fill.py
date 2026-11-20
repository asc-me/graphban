"""Project tile fill: accent2 and pattern (GRPH-1746).

`accent` stays the one solid colour. `accent2` is the ombre's second colour
(null = solid). `pattern` is the overlay, default `none`.

Revision ID: 0216
Revises: 0215
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0216"
down_revision: Union[str, None] = "0215"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("accent2", sa.String(), nullable=True))
    op.add_column("projects", sa.Column(
        "pattern", sa.String(), nullable=False, server_default="none"))


def downgrade() -> None:
    op.drop_column("projects", "pattern")
    op.drop_column("projects", "accent2")
