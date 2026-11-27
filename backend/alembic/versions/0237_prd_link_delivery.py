"""PRD link delivery cursors (GRPH-P104 S3): what each side's PRD has been told about.

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


def upgrade() -> None:
    op.add_column("prd_links", sa.Column("seen_a_message_id", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("prd_links", sa.Column("seen_b_message_id", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("prd_links", sa.Column("request_seen", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("prd_link_messages", sa.Column("author_side", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("prd_link_messages", "author_side")
    op.drop_column("prd_links", "request_seen")
    op.drop_column("prd_links", "seen_b_message_id")
    op.drop_column("prd_links", "seen_a_message_id")
