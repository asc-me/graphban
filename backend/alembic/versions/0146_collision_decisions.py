"""`collision_decisions`: persisted answers to collision clusters (PRD-60 D8 / GRPH-1138).

Revision ID: 0146
Revises: 0145
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0146"
down_revision: Union[str, None] = "0145"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collision_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("member_key", sa.String(), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("order", sa.JSON(), nullable=False),
        sa.Column("links_added", sa.JSON(), nullable=False),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_collision_decisions_project_id", "collision_decisions", ["project_id"])
    op.create_index("ix_collision_decisions_member_key", "collision_decisions", ["member_key"])


def downgrade() -> None:
    op.drop_index("ix_collision_decisions_member_key", table_name="collision_decisions")
    op.drop_index("ix_collision_decisions_project_id", table_name="collision_decisions")
    op.drop_table("collision_decisions")
