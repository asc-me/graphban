"""item_resolution: close an item as duplicate or superseded (GRPH-1671).

Revision ID: 0203
Revises: 0202
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0203"
down_revision: Union[str, None] = "0202"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("resolution", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "resolution")
