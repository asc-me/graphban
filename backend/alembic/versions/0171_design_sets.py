"""Design sets and their screens (GRPH-1279 / GRPH-P64).

Revision ID: 0171
Revises: 0170
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0171"
down_revision: Union[str, None] = "0170"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "design_sets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("prd_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("stale_reason", sa.String(), server_default="", nullable=False),
        sa.Column("stale_detail", sa.JSON(), nullable=True),
        sa.Column("preamble_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("preamble_hash", sa.String(), server_default="", nullable=False),
        sa.Column("prd_hash", sa.String(), server_default="", nullable=False),
        sa.Column("prd_baselined", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("constraints_body", sa.Text(), server_default="", nullable=False),
        sa.Column("constraints_hash", sa.String(), server_default="", nullable=False),
        sa.Column("constraints_accepted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_design_sets_project_id", "design_sets", ["project_id"])
    op.create_index("ix_design_sets_prd_id", "design_sets", ["prd_id"])
    op.create_table(
        "design_screens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("design_set_id", sa.String(), sa.ForeignKey("design_sets.id"), nullable=False),
        sa.Column("screen", sa.String(), nullable=False),
        sa.Column("routes", sa.JSON(), nullable=True),
        sa.Column("tier", sa.String(), server_default="UNBACKED", nullable=False),
        sa.Column("tier_confirmed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("tier_note", sa.Text(), server_default="", nullable=False),
        sa.Column("tier_proposal", sa.String(), server_default="", nullable=False),
        sa.Column("tier_proposal_basis", sa.String(), server_default="", nullable=False),
        sa.Column("confirmed_by", sa.String(), server_default="", nullable=False),
        sa.Column("prompt_hash", sa.String(), server_default="", nullable=False),
        sa.Column("canvas_hash", sa.String(), server_default="", nullable=False),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.UniqueConstraint("design_set_id", "screen", name="uq_design_screen"),
    )
    op.create_index("ix_design_screens_design_set_id", "design_screens", ["design_set_id"])


def downgrade() -> None:
    op.drop_index("ix_design_screens_design_set_id", table_name="design_screens")
    op.drop_table("design_screens")
    op.drop_index("ix_design_sets_prd_id", table_name="design_sets")
    op.drop_index("ix_design_sets_project_id", table_name="design_sets")
    op.drop_table("design_sets")
