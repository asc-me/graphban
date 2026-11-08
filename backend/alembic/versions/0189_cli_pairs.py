"""`cli_pairs` — device-code sign-in for `gban login --pair` (GRPH-1366).

Revision ID: 0189
Revises: 0188
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0189"
down_revision: Union[str, None] = "0188"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cli_pairs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("device_hash", sa.String(), nullable=False),
        sa.Column("user_code", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cli_pairs_device_hash", "cli_pairs", ["device_hash"], unique=True)
    op.create_index("ix_cli_pairs_user_code", "cli_pairs", ["user_code"])


def downgrade() -> None:
    op.drop_index("ix_cli_pairs_user_code", table_name="cli_pairs")
    op.drop_index("ix_cli_pairs_device_hash", table_name="cli_pairs")
    op.drop_table("cli_pairs")
