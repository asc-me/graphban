"""PRD links and their threads (GRPH-P104 S1): prd_links, prd_link_messages and the org's
prd_link_accept_agents setting.

Revision ID: 0236
Revises: 0235
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0236"
down_revision: Union[str, None] = "0235"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column(
        "prd_link_accept_agents", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "prd_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("prd_a_id", sa.String(), nullable=False),
        sa.Column("prd_b_id", sa.String(), nullable=False),
        sa.Column("project_a_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("project_b_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("decision_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("prd_a_id", "prd_b_id", name="uq_prd_link_pair"),
    )
    for col in ("prd_a_id", "prd_b_id", "project_a_id", "project_b_id"):
        op.create_index(f"ix_prd_links_{col}", "prd_links", [col])
    op.create_table(
        "prd_link_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("link_id", sa.String(), sa.ForeignKey("prd_links.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_kind", sa.String(), nullable=False),
        sa.Column("author_label", sa.String(), nullable=False, server_default=""),
        sa.Column("author_owner", sa.String(), nullable=False, server_default=""),
        sa.Column("cites", sa.JSON(), nullable=False),
        sa.Column("reply_to_id", sa.Integer(), sa.ForeignKey("prd_link_messages.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_prd_link_messages_link_id", "prd_link_messages", ["link_id"])


def downgrade() -> None:
    op.drop_index("ix_prd_link_messages_link_id", table_name="prd_link_messages")
    op.drop_table("prd_link_messages")
    for col in ("project_b_id", "project_a_id", "prd_b_id", "prd_a_id"):
        op.drop_index(f"ix_prd_links_{col}", table_name="prd_links")
    op.drop_table("prd_links")
    op.drop_column("organizations", "prd_link_accept_agents")
