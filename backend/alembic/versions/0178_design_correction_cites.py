"""`design_corrections.cited_path` / `cited_hash`: the code a correction quotes (GRPH-1308).

Revision ID: 0178
Revises: 0177
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0178"
down_revision: Union[str, None] = "0177"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("design_corrections") as batch:
        batch.add_column(sa.Column("cited_path", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("cited_hash", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("design_corrections") as batch:
        batch.drop_column("cited_hash")
        batch.drop_column("cited_path")
