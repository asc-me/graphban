"""Nullable gitops_model on organizations and projects (PRD-32 slice 1).

NULL = Unmeasured = no model chosen. Not a seventh live process field that
resolve() prefers over the six; it is how the operator last applied a preset.

Hand-written: --autogenerate sweeps unrelated drift and gets EmbeddingType
wrong (same reason 0095 was hand-written).

Revision ID: 0096
Revises: 0095
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0096"
down_revision: Union[str, None] = "0095"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("gitops_model", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("gitops_model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "gitops_model")
    op.drop_column("organizations", "gitops_model")
