"""GRPH-945: record the reviewer's tier, vendor and model on the item at sign-off.

Revision ID: 0132
Revises: 0131
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0132"
down_revision: Union[str, None] = "0131"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("reviewed_by_capabilities", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "reviewed_by_capabilities")
