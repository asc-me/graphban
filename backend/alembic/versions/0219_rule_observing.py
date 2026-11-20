"""Observing rules: would-have outcomes and the toggle's observing_since (GRPH-1777, P91 D5).

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
    with op.batch_alter_table("rule_toggles") as b:
        b.add_column(sa.Column("observing_since", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "rule_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("commit", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rule_observations_project_id", "rule_observations", ["project_id"])
    op.create_index("ix_rule_observations_rule_id", "rule_observations", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_rule_observations_rule_id", table_name="rule_observations")
    op.drop_index("ix_rule_observations_project_id", table_name="rule_observations")
    op.drop_table("rule_observations")
    with op.batch_alter_table("rule_toggles") as b:
        b.drop_column("observing_since")
