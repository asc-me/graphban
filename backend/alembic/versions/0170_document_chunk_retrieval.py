"""Document chunk retrieval: kind, PRD citation, embedding (GRPH-1277 / GRPH-P64).

A PRD baseline chunk has no document row, so `document_id` becomes nullable and the chunk
carries the PRD id and version it cites. The vector is a cache of `text`.

Revision ID: 0170
Revises: 0169
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0170"
down_revision: Union[str, None] = "0169"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    with op.batch_alter_table("document_chunks") as b:
        b.alter_column("document_id", existing_type=sa.String(), nullable=True)
        b.add_column(sa.Column("project_id", sa.String(), server_default="", nullable=False))
        b.add_column(sa.Column("kind", sa.String(), server_default="", nullable=False))
        b.add_column(sa.Column("prd_id", sa.String(), nullable=True))
        b.add_column(sa.Column("version", sa.String(), server_default="", nullable=False))
    op.create_index("ix_document_chunks_project_id", "document_chunks", ["project_id"])
    op.create_index("ix_document_chunks_prd_id", "document_chunks", ["prd_id"])
    # Existing rows are document chunks: give them their kind and project.
    op.execute(
        "UPDATE document_chunks SET "
        "kind = (SELECT kind FROM documents WHERE documents.id = document_chunks.document_id), "
        "project_id = (SELECT project_id FROM documents WHERE documents.id = document_chunks.document_id)")
    if pg:
        dim = int(settings.embed_dim)
        op.execute(f"ALTER TABLE document_chunks ADD COLUMN embedding vector({dim})")
        op.execute("CREATE INDEX ix_document_chunks_embedding ON document_chunks "
                   "USING hnsw (embedding vector_cosine_ops)")
    else:
        op.add_column("document_chunks", sa.Column("embedding", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.drop_column("document_chunks", "embedding")
    op.drop_index("ix_document_chunks_prd_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_project_id", table_name="document_chunks")
    with op.batch_alter_table("document_chunks") as b:
        b.drop_column("version")
        b.drop_column("prd_id")
        b.drop_column("kind")
        b.drop_column("project_id")
        b.alter_column("document_id", existing_type=sa.String(), nullable=False)
