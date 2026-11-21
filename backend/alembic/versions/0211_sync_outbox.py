"""sync outbox: per-entity changes for projects at sync_level `sync` (GRPH-1733).

Adds `sync_project.sync_level` (default `digests`, so every existing mapping stays on
today's push), `cloud_permission` (default `comments`), and `enabled_by` / `enabled_at`
for the audit. `sync_outbox` is one row per entity. `sync_manifest` is what the console
has confirmed. No network call.

Revision ID: 0211
Revises: 0210
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0211"
down_revision: Union[str, None] = "0210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sync_project") as batch:
        batch.add_column(sa.Column(
            "sync_level", sa.String(), nullable=False, server_default="digests"))
        batch.add_column(sa.Column(
            "cloud_permission", sa.String(), nullable=False, server_default="comments"))
        batch.add_column(sa.Column("enabled_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "sync_outbox",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hash", sa.String(), nullable=False, server_default=""),
        sa.Column("dirty_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_dirty_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parked_reason", sa.String(), nullable=True),
        sa.Column("parked_contract", sa.String(), nullable=True),
        sa.Column("change_class", sa.String(), nullable=False, server_default="edit"),
        sa.PrimaryKeyConstraint("project_id", "entity_type", "entity_id",
                                 name="pk_sync_outbox"),
    )
    op.create_table(
        "sync_manifest",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hash", sa.String(), nullable=False, server_default=""),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project_id", "entity_type", "entity_id",
                                 name="pk_sync_manifest"),
    )


def downgrade() -> None:
    op.drop_table("sync_manifest")
    op.drop_table("sync_outbox")
    with op.batch_alter_table("sync_project") as batch:
        batch.drop_column("enabled_at")
        batch.drop_column("enabled_by")
        batch.drop_column("cloud_permission")
        batch.drop_column("sync_level")
