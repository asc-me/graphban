"""PRD link suggestions (GRPH-P104 S2): the sending project and the one decision on a message.

Revision ID: 0237
Revises: 0236
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0237"
down_revision: Union[str, None] = "0236"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TEXT = ("project_id", "outcome", "outcome_code", "outcome_message", "outcome_item_id", "decided_by")


def upgrade() -> None:
    for col in _TEXT:
        op.add_column("prd_link_messages", sa.Column(
            col, sa.Text() if col == "outcome_message" else sa.String(),
            nullable=False, server_default=""))
    op.add_column("prd_link_messages", sa.Column(
        "decided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("prd_link_messages", "decided_at")
    for col in reversed(_TEXT):
        op.drop_column("prd_link_messages", col)
