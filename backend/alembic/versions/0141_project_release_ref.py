"""A project's release ref, set on the box (GRPH-1109 / PRD-P57 S2).

One nullable literal branch name. NULL is unmeasured: no backfill, because inferring
`main` or the gitops base branch for existing projects is exactly what the slice forbids.

Revision ID: 0141
Revises: 0140
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0141"
down_revision: Union[str, None] = "0140"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("release_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "release_ref")
