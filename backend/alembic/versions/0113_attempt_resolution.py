"""The resolution that produced an attempt (PRD-38 D7).

A recommendation card is worth nothing without a replay, and a replay that re-ran today's
resolver over last month's attempts would be answering with a matrix, a profile and an
installed set that have all moved since. So the resolution is recorded when it happens —
every candidate's score and status, and every drop with the score it would have had — and the
replay re-ranks what actually happened.

Revision ID: 0113
Revises: 0112
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0113"
down_revision: Union[str, None] = "0112"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("attempt_telemetry", sa.Column("resolution", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("attempt_telemetry", "resolution")
