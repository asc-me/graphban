"""`design_canvases.members` and `root`: a zip canvas records its files (GRPH-1306).

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
    with op.batch_alter_table("design_canvases") as batch:
        batch.add_column(sa.Column("members", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("root", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("design_canvases") as batch:
        batch.drop_column("root")
        batch.drop_column("members")
