"""`rule_suggestion_dismissals`: per-project dismissed Recommended rules (GRPH-1160).

Revision ID: 0151
Revises: 0150
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0151"
down_revision: Union[str, None] = "0150"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rule_suggestion_dismissals",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("suggestion_id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False, server_default=""),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rule_suggestion_dismissals")
