"""`auto_update_config`: opt-in auto-update windows for this box (GRPH-1299).

Revision ID: 0175
Revises: 0174
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0175"
down_revision: Union[str, None] = "0174"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL_DAYS = "[0, 1, 2, 3, 4, 5, 6]"


def upgrade() -> None:
    op.create_table(
        "auto_update_config",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("check_days", sa.JSON(), nullable=False, server_default=_ALL_DAYS),
        sa.Column("check_start", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("check_end", sa.String(5), nullable=False, server_default="05:00"),
        sa.Column("install_days", sa.JSON(), nullable=False, server_default=_ALL_DAYS),
        sa.Column("install_start", sa.String(5), nullable=False, server_default="02:00"),
        sa.Column("install_end", sa.String(5), nullable=False, server_default="05:00"),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_state", sa.String(16), nullable=False, server_default=""),
        sa.Column("last_check_tag", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_auto_install_tag", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_auto_install_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_auto_install_outcome", sa.String(16), nullable=False,
                  server_default=""),
        sa.Column("last_auto_install_error", sa.String(400), nullable=False,
                  server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("auto_update_config")
