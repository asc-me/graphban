"""Linked boxes pull org rule decisions: `rule_decision_pull` (box) and `box_rule_syncs` and
`org_rule_versions` (cloud) (GRPH-1297).

Revision ID: 0177
Revises: 0176
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0177"
down_revision: Union[str, None] = "0176"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rule_decision_pull",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("applied_version", sa.String(), nullable=False, server_default=""),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile", sa.String(), nullable=True),
        sa.Column("profile_version", sa.String(), nullable=True),
        sa.Column("reported", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "box_rule_syncs",
        sa.Column("key_id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("applied_version", sa.String(), nullable=False, server_default=""),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_box_rule_syncs_org_id", "box_rule_syncs", ["org_id"])
    op.create_table(
        "org_rule_versions",
        sa.Column("org_id", sa.String(), primary_key=True),
        sa.Column("version", sa.String(), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("org_rule_versions")
    op.drop_index("ix_box_rule_syncs_org_id", table_name="box_rule_syncs")
    op.drop_table("box_rule_syncs")
    op.drop_table("rule_decision_pull")
