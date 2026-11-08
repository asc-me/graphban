"""`projects.repo_url` — the repository a project belongs to (GRPH-1365).

Nullable and never backfilled: NULL means nobody said, which `gban setup` reads as "fall back to
the name match", not as "this project has no repository".

Revision ID: 0188
Revises: 0187
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0188"
down_revision: Union[str, None] = "0187"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("repo_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "repo_url")
