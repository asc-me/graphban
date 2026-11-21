"""Org-scope dismissal of a rule proposal (GRPH-1808).

An org admin dismisses a proposal for the org only: who, when and why are recorded on the
proposal, whose project status stays `open`. All three are nullable; no backfill.

Revision ID: 0225
Revises: 0224
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0225"
down_revision: Union[str, None] = "0224"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("rule_proposals") as b:
        b.add_column(sa.Column("org_dismissed_by", sa.String(), nullable=True))
        b.add_column(sa.Column("org_dismissed_reason", sa.Text(), nullable=True))
        b.add_column(sa.Column("org_dismissed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rule_proposals") as b:
        b.drop_column("org_dismissed_at")
        b.drop_column("org_dismissed_reason")
        b.drop_column("org_dismissed_by")
