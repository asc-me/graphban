"""Projects can be archived (GRPH-1955): archived_at, archived_reason, archived_by.

Every existing project stays live: archived_at NULL, reason "".

Revision ID: 0232
Revises: 0231
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0232"
down_revision: Union[str, None] = "0231"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("archived_reason", sa.Text(), nullable=False,
                                        server_default=""))
    op.add_column("projects", sa.Column("archived_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "archived_by")
    op.drop_column("projects", "archived_reason")
    op.drop_column("projects", "archived_at")
