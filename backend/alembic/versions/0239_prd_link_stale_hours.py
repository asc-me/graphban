"""Org setting: how old an undecided PRD-link thing may get (GRPH-P104 S5).

Revision ID: 0239
Revises: 0238
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0239"
down_revision: Union[str, None] = "0238"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column(
        "prd_link_stale_hours", sa.Integer(), nullable=False, server_default="72"))


def downgrade() -> None:
    op.drop_column("organizations", "prd_link_stale_hours")
