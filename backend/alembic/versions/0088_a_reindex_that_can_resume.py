"""Per-table re-index progress (PRD-25 S4b, GRPH-536).

The slice originally specified `reindex_total` / `reindex_done` — two integers on the
deployment row. The grill amended it to per-table, because one counter cannot distinguish a
crash after finishing `memory_shards` from a crash partway through it, and the restart then has
to choose between redoing finished work and skipping unfinished work.

A row per (scope, table). Deliberately not a job table: no queue, no worker identity, no
history. How many rows there are and how many are done is the whole of what a resume needs.

Revision ID: 0088
Revises: 0087
"""
import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reindex_progress",
        sa.Column("scope", sa.String(), primary_key=True),
        sa.Column("table_name", sa.String(), primary_key=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("reindex_progress")
