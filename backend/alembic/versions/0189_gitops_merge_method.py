"""`gitops_merge_method` on projects and organizations (GRPH-1428).

Nullable and never backfilled: NULL is unmeasured, which the supervisor reads as `squash`.

Revision ID: 0189
Revises: 0188
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0189"
down_revision: Union[str, None] = "0188"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("gitops_merge_method", sa.String(), nullable=True))
    op.add_column("organizations", sa.Column("gitops_merge_method", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "gitops_merge_method")
    op.drop_column("projects", "gitops_merge_method")
