"""Governance and data boundary (PRD-P10 / GRPH-194).

Adds `storage_tier` to tracker_links: controls what the hub stores for a linked
tracker. Default is "bodies_in_hub" (full mirror including descriptions); regulated
buyers can set "metadata_only" (IDs/state/assignee/labels/timestamps only — bodies
stay on the local spoke).

Under metadata_only, hub-side collision clustering is unavailable (a third answer,
not labels-as-cluster).

Revision ID: 0128
Revises: 0127
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0128"
down_revision: Union[str, None] = "0127"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracker_links",
        sa.Column(
            "storage_tier",
            sa.String(),
            nullable=False,
            server_default="bodies_in_hub",
        ),
    )


def downgrade() -> None:
    op.drop_column("tracker_links", "storage_tier")
