"""Sync engine — fingerprint store and tracker mirror (PRD-P10 / GRPH-188).

Two tables:
- sync_fingerprints: outbound write fingerprints for echo suppression.
  Keyed by (issue_id, field, expected_version). When the tracker webhook echoes
  a change we made, the fingerprint matches and the hub drops it.
- tracker_mirror: mirrored issue state from the external tracker. The hub stores
  the latest snapshot; reconcile diffs against this to detect external edits.

Revision ID: 0127
Revises: 0126
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0127"
down_revision: Union[str, None] = "0126"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_fingerprints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("link_id", sa.String(), sa.ForeignKey("tracker_links.id"), nullable=False, index=True),
        sa.Column("issue_id", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("expected_version", sa.String(), nullable=False),
        sa.Column("write_token", sa.String(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_sync_fingerprints_lookup",
        "sync_fingerprints",
        ["link_id", "issue_id", "field"],
    )

    op.create_table(
        "tracker_mirror",
        sa.Column("issue_id", sa.String(), primary_key=True),
        sa.Column("link_id", sa.String(), sa.ForeignKey("tracker_links.id"), nullable=False, index=True),
        sa.Column("tracker_kind", sa.String(), nullable=False, server_default="linear"),
        sa.Column("identifier", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("canonical_status", sa.String(), nullable=False, server_default="backlog"),
        sa.Column("assignee_id", sa.String(), nullable=True),
        sa.Column("assignee_name", sa.String(), nullable=False, server_default=""),
        sa.Column("labels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("tracker_updated_at", sa.String(), nullable=False, server_default=""),
        sa.Column("version", sa.String(), nullable=False, server_default=""),
        sa.Column("url", sa.String(), nullable=False, server_default=""),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("tracker_mirror")
    op.drop_table("sync_fingerprints")
