"""Design review loop: corrections, findings, canvas bindings, acceptances (GRPH-1280 / GRPH-P64).

Revision ID: 0172
Revises: 0171
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0172"
down_revision: Union[str, None] = "0171"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "design_corrections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("design_set_id", sa.String(), sa.ForeignKey("design_sets.id"), nullable=False),
        sa.Column("screen", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("design_set_id", "screen", "text_hash", name="uq_design_correction"),
    )
    op.create_index("ix_design_corrections_design_set_id", "design_corrections", ["design_set_id"])
    op.create_table(
        "design_findings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("design_set_id", sa.String(), sa.ForeignKey("design_sets.id"), nullable=False),
        sa.Column("screen", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(), nullable=False),
        sa.Column("canvas_hash", sa.String(), server_default="", nullable=False),
        sa.Column("winner", sa.String(), server_default="", nullable=False),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("correction_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("resolved_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("design_set_id", "screen", "text_hash", name="uq_design_finding"),
    )
    op.create_index("ix_design_findings_design_set_id", "design_findings", ["design_set_id"])
    op.create_table(
        "design_canvases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("design_set_id", sa.String(), sa.ForeignKey("design_sets.id"), nullable=False),
        sa.Column("screen", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("design_set_id", "screen", "content_hash", "prompt_hash",
                            name="uq_design_canvas"),
    )
    op.create_index("ix_design_canvases_design_set_id", "design_canvases", ["design_set_id"])
    op.create_table(
        "design_acceptances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("design_set_id", sa.String(), sa.ForeignKey("design_sets.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("screen", sa.String(), server_default="", nullable=False),
        sa.Column("prompt_hash", sa.String(), server_default="", nullable=False),
        sa.Column("prd_hash", sa.String(), server_default="", nullable=False),
        sa.Column("preamble_hash", sa.String(), server_default="", nullable=False),
        sa.Column("person", sa.String(), nullable=False),
        sa.Column("relayed_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_design_acceptances_design_set_id", "design_acceptances", ["design_set_id"])


def downgrade() -> None:
    for t in ("design_acceptances", "design_canvases", "design_findings", "design_corrections"):
        op.drop_index(f"ix_{t}_design_set_id", table_name=t)
        op.drop_table(t)
