"""Bound seats (PRD-36 D1).

A seat may carry the item its child will claim at registration and the delegation that
asked for it. Both nullable; an unbound seat is unchanged.

Revision ID: 0107
Revises: 0106
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0107"
down_revision: Union[str, None] = "0106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrolments", sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"),
                                          nullable=True))
    op.add_column("enrolments", sa.Column("delegation_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("enrolments", "delegation_id")
    op.drop_column("enrolments", "item_id")
