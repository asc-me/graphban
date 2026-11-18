"""maintenance_line: the `lines` table and `items.line_id` (GRPH-1681).

Revision ID: 0206
Revises: 0205
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0206"
down_revision: Union[str, None] = "0205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default="Maintenance"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", name="uq_lines_project_id"),
        sa.UniqueConstraint("project_id", "number", name="uq_line_project_number"),
    )
    op.add_column("items", sa.Column("line_id", sa.String(), nullable=True))
    op.create_index("ix_items_line_id", "items", ["line_id"])


def downgrade() -> None:
    op.drop_index("ix_items_line_id", table_name="items")
    op.drop_column("items", "line_id")
    op.drop_table("lines")
