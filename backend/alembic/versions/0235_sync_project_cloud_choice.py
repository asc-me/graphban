"""The console's per-project sync choice on the box (GRPH-1978): sync_project.console_sync,
console_home and reported_sync.

Every existing mapping starts with all three null: not read from the console yet, home
unknown (push as before), and no effective level reported — so no holder field goes out
until the box has read the console's choice and reported the effective level.

Revision ID: 0235
Revises: 0234
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0235"
down_revision: Union[str, None] = "0234"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_project", sa.Column("console_sync", sa.String(), nullable=True))
    op.add_column("sync_project", sa.Column("console_home", sa.Boolean(), nullable=True))
    op.add_column("sync_project", sa.Column("reported_sync", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_project", "reported_sync")
    op.drop_column("sync_project", "console_home")
    op.drop_column("sync_project", "console_sync")
