"""What each harness loads, so the Harness page can show where they differ.

Revision ID: 0133
Revises: 0132
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0133"
down_revision: Union[str, None] = "0132"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "harness_surfaces",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("host", sa.String(length=128), primary_key=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("harness_surfaces")
