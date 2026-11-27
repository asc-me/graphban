"""PRD link delivery cursors (GRPH-P104 S3): prd_link_reads.

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
    op.create_table(
        "prd_link_reads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("link_id", sa.String(), sa.ForeignKey("prd_links.id"), nullable=False),
        sa.Column("prd_id", sa.String(), nullable=False),
        sa.Column("last_message_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("link_id", "prd_id", name="uq_prd_link_read"),
    )
    op.create_index("ix_prd_link_reads_link_id", "prd_link_reads", ["link_id"])


def downgrade() -> None:
    op.drop_index("ix_prd_link_reads_link_id", table_name="prd_link_reads")
    op.drop_table("prd_link_reads")
