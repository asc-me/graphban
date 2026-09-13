"""Tracker link record with authority flag and field mapping (GRPH-186 / PRD-10).

Links an external tracker (Linear first) to an AgentLedger project at org scope.
The tracker is authoritative: AgentLedger mirrors read-heavy and writes back only
canonical status transitions, comments/links, and the triage-board assignee.

Revision ID: 0125
Revises: 0124
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0125"
down_revision: Union[str, None] = "0124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tracker_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("tracker_kind", sa.String(), nullable=False, server_default="linear"),
        sa.Column("tracker_team_id", sa.String(), nullable=False),
        sa.Column("tracker_team_name", sa.String(), nullable=False, server_default=""),
        sa.Column("authority", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("field_mapping", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("write_back_comment", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "tracker_kind", "tracker_team_id",
                            name="uq_tracker_link_team"),
    )


def downgrade() -> None:
    op.drop_table("tracker_links")
