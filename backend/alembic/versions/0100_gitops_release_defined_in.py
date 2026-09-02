"""Where this repo's release process is written (P32 locator).

A path or URL agents read from get_context. NULL is unmeasured, not
`docs/release.md`. Not a product CalVer — that lives on Updates.

Hand-written: `--autogenerate` sweeps unrelated drift.

Revision ID: 0100
Revises: 0099
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0100"
down_revision: Union[str, None] = "0099"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("gitops_release_defined_in", sa.String(), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("gitops_release_defined_in", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "gitops_release_defined_in")
    op.drop_column("organizations", "gitops_release_defined_in")
