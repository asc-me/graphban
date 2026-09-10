"""Widen attempt_telemetry.exit_meaning so the real gbagent wording fits.

VARCHAR(64) truncated the budget-exhaust sentence (76 chars) on Postgres;
SQLite does not enforce the length, so the utilization fixture stayed green
and production could not store the fact §7.3 / criterion 17 reads.
`_is_budget_hit` still matches the wording.

Revision ID: 0122
Revises: 0121
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0122"
down_revision: Union[str, None] = "0121"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "attempt_telemetry",
        "exit_meaning",
        existing_type=sa.String(64),
        type_=sa.String(256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "attempt_telemetry",
        "exit_meaning",
        existing_type=sa.String(256),
        type_=sa.String(64),
        existing_nullable=True,
    )
