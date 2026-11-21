"""Publish destination on the org and the box (GRPH-1761).

The org stores a URL and a branch. The box stores the pair it is publishing to
(local path, or the URL it pulled) plus the previous path a link replaced.
Empty strings are unconfigured. `publish_enabled` defaults off; this migration
does not add a way to turn it on.

Revision ID: 0215
Revises: 0214
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0215"
down_revision: Union[str, None] = "0214"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column(
        "publish_repo_url", sa.String(length=1024), nullable=False, server_default=""))
    op.add_column("organizations", sa.Column(
        "publish_branch", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "repo_path", sa.String(length=1024), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "repo_branch", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "previous_path", sa.String(length=1024), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "received_url", sa.String(length=1024), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "received_branch", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "destination_source", sa.String(length=16), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "publish_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("sync_storage_config", sa.Column(
        "destination_pull_state", sa.String(length=16), nullable=False, server_default=""))
    op.add_column("sync_storage_config", sa.Column(
        "destination_pull_reason", sa.String(length=400), nullable=False, server_default=""))


def downgrade() -> None:
    for name in (
        "destination_pull_reason", "destination_pull_state", "publish_enabled",
        "destination_source", "received_branch", "received_url", "previous_path",
        "repo_branch", "repo_path",
    ):
        op.drop_column("sync_storage_config", name)
    op.drop_column("organizations", "publish_branch")
    op.drop_column("organizations", "publish_repo_url")
