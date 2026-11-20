"""sync_project.pending_purge: a purge the console has not confirmed (GRPH-1738).

Null on every existing row. Those rows are not mid-purge; a default of empty
string would read as a marker that nobody set.

Revision ID: 0215
Revises: 0214
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0215"
down_revision: Union[str, None] = "0214"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_project", sa.Column("pending_purge", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_project", "pending_purge")
