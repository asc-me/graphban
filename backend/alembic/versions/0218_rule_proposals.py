"""Rule proposals: cited, direction-checked suggestions an agent may file (GRPH-1775, P91 D2).

Revision ID: 0218
Revises: 0217
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0218"
down_revision: Union[str, None] = "0217"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rule_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("primitive", sa.String(), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("proposer", sa.String(), nullable=False),
        sa.Column("proposer_kind", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("loosens", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rule_proposals_project_id", "rule_proposals", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_rule_proposals_project_id", table_name="rule_proposals")
    op.drop_table("rule_proposals")
