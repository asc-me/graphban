"""`rule_toggles`: per-scope state and params for library rules (GRPH-1262, PRD-P63 D4).

Revision ID: 0167
Revises: 0166
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0167"
down_revision: Union[str, None] = "0166"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rule_toggles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("on", sa.Boolean(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(org_id IS NULL) <> (project_id IS NULL)",
                           name="ck_rule_toggles_one_scope"),
        sa.CheckConstraint("project_id IS NULL OR NOT locked",
                           name="ck_rule_toggles_lock_is_org_only"),
        sa.UniqueConstraint("rule_id", "org_id", name="uq_rule_toggles_org"),
        sa.UniqueConstraint("rule_id", "project_id", name="uq_rule_toggles_project"),
    )
    op.create_index("ix_rule_toggles_rule_id", "rule_toggles", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_rule_toggles_rule_id", table_name="rule_toggles")
    op.drop_table("rule_toggles")
