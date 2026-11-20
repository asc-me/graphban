"""Rule guidance and primitive-gap counts (GRPH-1780, P91 D8).

Revision ID: 0219
Revises: 0218
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0219"
down_revision: Union[str, None] = "0218"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rule_guidance",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("proposer", sa.String(), nullable=False),
        sa.Column("proposer_kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rule_guidance_project_id", "rule_guidance", ["project_id"])
    op.create_table(
        "primitive_gap_counts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("project_id", "stage", "label"),
    )
    op.create_index("ix_primitive_gap_counts_project_id", "primitive_gap_counts", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_primitive_gap_counts_project_id", table_name="primitive_gap_counts")
    op.drop_table("primitive_gap_counts")
    op.drop_index("ix_rule_guidance_project_id", table_name="rule_guidance")
    op.drop_table("rule_guidance")
