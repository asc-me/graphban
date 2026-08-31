"""Sparse gitops columns on organizations and projects.

Hand-written for the reason 0077-0080 and 0093/0094 were: `--autogenerate` sweeps in
unrelated drift (and gets EmbeddingType/pgvector columns wrong).

NULL on every column, including the boolean — a server_default of false would make every
existing org "you may push to the base".

Revision ID: 0095
Revises: 0094
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0095"
down_revision: Union[str, None] = "0094"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    "gitops_base_branch",
    "gitops_no_push_to_base",
    "gitops_branch_name_pattern",
    "gitops_pr_title_pattern",
    "gitops_reviewer_bar",
    "gitops_version_scheme",
)


def _add(table: str) -> None:
    op.add_column(table, sa.Column("gitops_base_branch", sa.String(), nullable=True))
    op.add_column(table, sa.Column("gitops_no_push_to_base", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("gitops_branch_name_pattern", sa.String(), nullable=True))
    op.add_column(table, sa.Column("gitops_pr_title_pattern", sa.String(), nullable=True))
    op.add_column(table, sa.Column("gitops_reviewer_bar", sa.String(), nullable=True))
    op.add_column(table, sa.Column("gitops_version_scheme", sa.String(), nullable=True))


def upgrade() -> None:
    _add("organizations")
    _add("projects")


def downgrade() -> None:
    for table in ("projects", "organizations"):
        for col in _COLUMNS:
            op.drop_column(table, col)
