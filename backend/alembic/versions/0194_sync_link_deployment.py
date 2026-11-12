"""sync_link per deployment + sync_project; key links migrate in place (GRPH-1517, P67 §6).

`sync_link` gains the deployment link a device-code sign-in writes (`console_url`,
`linked_email`, `token_enc`, `pending_device_enc`) and `legacy_key`. `sync_project` maps a
local project to its cloud project; a project with no row is local only.

**Migration (L6).** Each existing key link becomes a deployment link with `legacy_key` set to
its (still encrypted) key and one `sync_project` row for the project the key pushed. The key
keeps pushing until the engineer signs in. Nothing here calls the console, so the migration
cannot fail on a network, and nothing is decrypted, so it needs no SECRET_ENCRYPTION_KEY.

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

_NEW_LINK_COLUMNS = ("console_url", "linked_email", "token_enc", "pending_device_enc",
                     "legacy_key")


def pinned_project(bind) -> str | None:
    """The local project a key link pushed. A sync key is pinned to one cloud project, and
    the box pushed one local project into it: the one with the latest confirmed push, else
    `core` (the push default), else the first project by id. None on a box with no projects."""
    pushed = bind.execute(sa.text(
        "SELECT s.project_id FROM code_sync_state s JOIN projects p ON p.id = s.project_id "
        "WHERE s.last_synced_at IS NOT NULL ORDER BY s.last_synced_at DESC, s.project_id "
        "LIMIT 1")).scalar()
    if pushed:
        return pushed
    if bind.execute(sa.text("SELECT 1 FROM projects WHERE id = 'core'")).scalar():
        return "core"
    return bind.execute(sa.text(
        "SELECT id FROM projects ORDER BY id LIMIT 1")).scalar()


def migrate_key_links(bind) -> int:
    """Turn each key link into a deployment link with `legacy_key` set, plus one mapped
    project. Idempotent: a row already carrying `legacy_key` or a token is left alone.
    Returns how many links were migrated."""
    rows = bind.execute(sa.text(
        "SELECT id, cloud_url, api_key_enc FROM sync_link "
        "WHERE cloud_url <> '' AND api_key_enc <> '' "
        "AND legacy_key = '' AND token_enc = ''")).all()
    for link_id, cloud_url, key_enc in rows:
        bind.execute(sa.text(
            "UPDATE sync_link SET legacy_key = :k, "
            "console_url = CASE WHEN console_url = '' THEN :u ELSE console_url END "
            "WHERE id = :id"), {"k": key_enc, "u": cloud_url, "id": link_id})
    if rows:
        project = pinned_project(bind)
        exists = project and bind.execute(sa.text(
            "SELECT 1 FROM sync_project WHERE local_project_id = :p"), {"p": project}).scalar()
        if project and not exists:
            bind.execute(sa.text(
                "INSERT INTO sync_project (local_project_id, cloud_project_id, created_at) "
                "VALUES (:p, '', CURRENT_TIMESTAMP)"), {"p": project})
    return len(rows)


def upgrade() -> None:
    for name in _NEW_LINK_COLUMNS:
        op.add_column("sync_link",
                      sa.Column(name, sa.String(), nullable=False, server_default=""))
    op.create_table(
        "sync_project",
        sa.Column("local_project_id", sa.String(), primary_key=True),
        sa.Column("cloud_project_id", sa.String(), nullable=False, server_default=""),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_result", sa.String(), nullable=True),
        sa.Column("last_push_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    migrate_key_links(op.get_bind())


def downgrade() -> None:
    op.drop_table("sync_project")
    for name in reversed(_NEW_LINK_COLUMNS):
        op.drop_column("sync_link", name)
