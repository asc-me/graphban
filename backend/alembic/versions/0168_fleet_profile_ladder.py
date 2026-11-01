"""Capacity-routing ladder on the fleet profile (GRPH-1255, PRD-62 D6).

`fleet_profiles.ladder` is a user's rung order (subscription / api_key / local). Null follows
the project policy's ladder. A preference: the reader keeps only rungs the policy allows.

Revision ID: 0168
Revises: 0167
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0168"
down_revision: Union[str, None] = "0167"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("fleet_profiles", sa.Column("ladder", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("fleet_profiles", "ladder")
