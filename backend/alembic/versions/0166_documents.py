"""Document record: documents + document_chunks (GRPH-1275 / GRPH-P64).

Ledger rows for the preamble, set constraints, prompts, review records and canvases. A PRD
keeps its own row; nothing here duplicates the spec.

Revision ID: 0166
Revises: 0165
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0166"
down_revision: Union[str, None] = "0165"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("prd_id", sa.String(), nullable=True),
        sa.Column("prd_hash", sa.String(), server_default="", nullable=False),
        sa.Column("set_id", sa.String(), server_default="", nullable=False),
        sa.Column("screen", sa.String(), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("stale_reason", sa.String(), server_default="", nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("governing", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("accepted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("blob_status", sa.String(), server_default="none", nullable=False),
        sa.Column("blob_key", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "kind", "set_id", "screen", "version",
                            name="uq_documents_identity"),
    )
    op.create_index("ix_documents_project_kind", "documents", ["project_id", "kind"])
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("heading", sa.String(), server_default="", nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("governing", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_project_kind", table_name="documents")
    op.drop_table("documents")
