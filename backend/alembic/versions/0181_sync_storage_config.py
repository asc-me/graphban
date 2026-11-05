"""`sync_storage_config`: the box's sync folder path and blob directory (GRPH-1320).

Revision ID: 0181
Revises: 0180
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0181"
down_revision: Union[str, None] = "0180"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_storage_config",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("folder_path", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("blob_dir", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("last_export_status", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sync_storage_config")
