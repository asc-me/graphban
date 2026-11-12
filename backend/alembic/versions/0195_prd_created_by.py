"""prds.created_by: the creating user, nullable, no backfill (GRPH-1526, P75 D2).

Revision ID: 0195
Revises: 0194
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0195"
down_revision: Union[str, None] = "0194"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("prds") as b:
        b.add_column(sa.Column("created_by", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("prds") as b:
        b.drop_column("created_by")
