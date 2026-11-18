"""remote_spec: specs published by other deployments, read-only here (GRPH-1677).

Revision ID: 0210
Revises: 0209
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0210"
down_revision: Union[str, None] = "0209"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "remote_spec",
        sa.Column("prd_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("home", sa.String(), nullable=False, server_default=""),
        sa.Column("base_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("pulled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_remote_spec_project_id", "remote_spec", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_remote_spec_project_id", table_name="remote_spec")
    op.drop_table("remote_spec")
