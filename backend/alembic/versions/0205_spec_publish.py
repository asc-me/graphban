"""spec_publish: the PRD versions this box last published to the console (GRPH-1676).

Revision ID: 0205
Revises: 0204
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0205"
down_revision: Union[str, None] = "0204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "spec_publish",
        sa.Column("prd_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False, server_default=""),
        sa.Column("base_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_spec_publish_project_id", "spec_publish", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_spec_publish_project_id", table_name="spec_publish")
    op.drop_table("spec_publish")
