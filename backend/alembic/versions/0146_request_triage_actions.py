"""Request triage actions: snooze/merge columns and the `request_actions` undo log (GRPH-1136).

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
    op.add_column("requests", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("requests", sa.Column("merged_into", sa.String(), nullable=True))
    op.create_table(
        "request_actions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("prior", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_request_actions_request_id", "request_actions", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_request_actions_request_id", table_name="request_actions")
    op.drop_table("request_actions")
    op.drop_column("requests", "merged_into")
    op.drop_column("requests", "snoozed_until")
