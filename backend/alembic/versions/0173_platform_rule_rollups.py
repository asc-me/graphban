"""`platform_rule_rollups`: opt-in platform effect of library rules (GRPH-1265, PRD-P63 D8).

Revision ID: 0173
Revises: 0172
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0173"
down_revision: Union[str, None] = "0172"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_rule_rollups",
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("rule_id", sa.String(), primary_key=True),
        sa.Column("orgs_contributing", sa.Integer(), nullable=False),
        sa.Column("top_org_share", sa.Float(), nullable=True),
        sa.Column("projects_on", sa.Integer(), nullable=False),
        sa.Column("caught", sa.Integer(), nullable=False),
        sa.Column("signed_before", sa.Integer(), nullable=False),
        sa.Column("escapes_before", sa.Integer(), nullable=False),
        sa.Column("signed_after", sa.Integer(), nullable=False),
        sa.Column("escapes_after", sa.Integer(), nullable=False),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_rule_rollups")
