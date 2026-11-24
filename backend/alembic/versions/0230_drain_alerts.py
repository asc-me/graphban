"""Drain notices ride the alerts table: nullable prd_id and pushed_at (GRPH-1913).

Existing `response_needed` rows keep both columns NULL.

Revision ID: 0230
Revises: 0229
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0230"
down_revision: Union[str, None] = "0229"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("prd_id", sa.String(), nullable=True))
    op.add_column("alerts", sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_alerts_prd_id", "alerts", ["prd_id"])


def downgrade() -> None:
    op.drop_index("ix_alerts_prd_id", table_name="alerts")
    op.drop_column("alerts", "pushed_at")
    op.drop_column("alerts", "prd_id")
