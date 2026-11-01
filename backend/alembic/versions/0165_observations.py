"""`observations`: typed facts about how work went (PRD-63 S1).

Revision ID: 0165
Revises: 0164
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0165"
down_revision: Union[str, None] = "0164"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=True),
        sa.Column("commit", sa.String(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False, server_default="live"),
        sa.Column("ref", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "dedupe_key", name="uq_observation_dedupe"),
    )
    op.create_index("ix_observations_project_id", "observations", ["project_id"])
    op.create_index("ix_observations_item_id", "observations", ["item_id"])
    op.create_index("ix_observations_project_kind", "observations", ["project_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_observations_project_kind", table_name="observations")
    op.drop_index("ix_observations_item_id", table_name="observations")
    op.drop_index("ix_observations_project_id", table_name="observations")
    op.drop_table("observations")
