"""PRD-41 S2: budget target, policy caps, capabilities at delegate.

`fleet_profiles.budget_tokens` is the soft per-sign-off target (D20). Policy `caps` live
inside the existing `projects.fleet_policy` JSON — no new column. `delegations` stores the
touchpoint-derived set at `delegate` time so the attempt row can keep both it and the
larger exit-time set.

Revision ID: 0120
Revises: 0119
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0120"
down_revision: Union[str, None] = "0119"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("fleet_profiles", sa.Column("budget_tokens", sa.Integer(), nullable=True))
    op.add_column("delegations", sa.Column("capabilities_at_delegate", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("delegations", "capabilities_at_delegate")
    op.drop_column("fleet_profiles", "budget_tokens")
