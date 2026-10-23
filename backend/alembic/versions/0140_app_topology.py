"""One app-topology record per project (GRPH-1098 / PRD-P55 §2).

A producer that holds the repo posts the map. One row per project; a valid post
replaces it and keeps the previous mapped sha for display. No row is unmapped,
which is not an empty ring of services.

Revision ID: 0140
Revises: 0139
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0140"
down_revision: Union[str, None] = "0139"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_topologies",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("producer_id", sa.String(), nullable=False),
        sa.Column("producer_version", sa.String(), nullable=False),
        sa.Column("mapped_sha", sa.String(), nullable=False),
        sa.Column("mapped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_mapped_sha", sa.String(), server_default="", nullable=False),
        sa.Column("posted_by", sa.String(), server_default="", nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("infrastructure", sa.JSON(), nullable=False),
        sa.Column("external_services", sa.JSON(), nullable=False),
        sa.Column("unresolved", sa.JSON(), nullable=False),
        sa.Column("edges", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("app_topologies")
