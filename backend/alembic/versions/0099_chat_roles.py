"""Per-task chat roles on a project (GRPH-316).

One credential per project is not enough: classify, critique, grill, and memory
judge need different models. A missing key inherits the project pointer, so
anyone who does not care keeps exactly one setting.

Revision ID: 0099
Revises: 0098
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0099"
down_revision: Union[str, None] = "0098"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("chat_roles", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("projects", "chat_roles")
