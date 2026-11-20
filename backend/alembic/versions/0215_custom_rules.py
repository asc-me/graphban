"""Custom rules: a rule a person writes from a library primitive (GRPH-1774, P91 D1).

Revision ID: 0215
Revises: 0214
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0215"
down_revision: Union[str, None] = "0214"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "custom_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("primitive", sa.String(), nullable=False),
        sa.Column("primitive_api", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("claims", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(org_id IS NULL) <> (project_id IS NULL)",
                           name="ck_custom_rules_one_scope"),
        sa.UniqueConstraint("rule_id", "org_id", name="uq_custom_rules_org"),
        sa.UniqueConstraint("rule_id", "project_id", name="uq_custom_rules_project"),
    )
    op.create_index("ix_custom_rules_rule_id", "custom_rules", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_custom_rules_rule_id", table_name="custom_rules")
    op.drop_table("custom_rules")
