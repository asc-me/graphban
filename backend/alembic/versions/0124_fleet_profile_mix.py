"""Harness mix on the fleet profile (GRPH-865).

`fleet_profiles.mix` is a preference: harness → share, normalised to 1. Null is
today's winner-take-all. Never a filter — that would be a policy cap.

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
    op.add_column("fleet_profiles", sa.Column("mix", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("fleet_profiles", "mix")
