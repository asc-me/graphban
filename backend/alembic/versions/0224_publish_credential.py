"""Publish credential on the box (GRPH-1762).

`credential_mode` is empty (not set), `file` (a path on this box) or `git` (the box's own
git credential for the remote). The path is stored on the box only. Both default to empty,
so an existing deployment has no credential and publishing stays off.

Revision ID: 0224
Revises: 0223
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0224"
down_revision: Union[str, None] = "0223"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_storage_config", sa.Column(
        "credential_mode", sa.String(length=8), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "credential_path", sa.String(length=1024), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("sync_storage_config", "credential_path")
    op.drop_column("sync_storage_config", "credential_mode")
