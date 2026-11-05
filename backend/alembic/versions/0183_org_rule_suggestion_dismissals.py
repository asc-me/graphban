"""Org-level dismissal of a Recommended-rules suggestion (GRPH-1326).

Revision ID: 0183
Revises: 0182
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0183"
down_revision: Union[str, None] = "0182"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_rule_suggestion_dismissals",
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("suggestion_id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False, server_default=""),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("org_rule_suggestion_dismissals")
