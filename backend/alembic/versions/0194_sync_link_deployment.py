"""sync_link per deployment + sync_project (GRPH-1517, P67 S8).

The singleton `sync_link` row gains the console deployment link (console_url, linked_email,
token_enc, device_code_enc) and a `legacy_key` marker; `sync_project` maps a local project to
its cloud project. Existing key links are migrated in place (legacy_key set, one mapped
project) and the console is never called.

Revision ID: 0194
Revises: 0193
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0194"
down_revision: Union[str, None] = "0193"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name in ("console_url", "linked_email", "token_enc", "device_code_enc", "legacy_key"):
        op.add_column("sync_link", sa.Column(name, sa.String(), nullable=False, server_default=""))
    op.create_table(
        "sync_project",
        sa.Column("local_project_id", sa.String(), primary_key=True),
        sa.Column("cloud_project_id", sa.String(), nullable=False, server_default=""),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_result", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("last_push_reason", sa.String(), nullable=False, server_default=""),
    )
    bind = op.get_bind()
    linked = bind.execute(sa.text(
        "SELECT 1 FROM sync_link WHERE api_key_enc <> '' AND cloud_url <> ''")).first()
    if linked is None:
        return
    bind.execute(sa.text(
        "UPDATE sync_link SET legacy_key = 'legacy', console_url = cloud_url "
        "WHERE api_key_enc <> '' AND cloud_url <> ''"))
    # The project the key was pinned to: the one this box has pushed, else the default.
    pushed = [r[0] for r in bind.execute(sa.text(
        "SELECT project_id FROM code_sync_state WHERE last_synced_at IS NOT NULL "
        "ORDER BY last_synced_at DESC LIMIT 1"))]
    if not pushed:
        pushed = [r[0] for r in bind.execute(sa.text("SELECT id FROM projects WHERE id = 'core'"))]
    for pid in pushed:
        bind.execute(sa.text(
            "INSERT INTO sync_project (local_project_id, cloud_project_id, last_push_result, "
            "last_push_reason) VALUES (:p, '', 'unknown', '')"), {"p": pid})


def downgrade() -> None:
    op.drop_table("sync_project")
    for name in ("legacy_key", "device_code_enc", "token_enc", "linked_email", "console_url"):
        op.drop_column("sync_link", name)
