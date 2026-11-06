"""Per-screen brief on design_screens (GRPH-1354).

Revision ID: 0184
Revises: 0183
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0184"
down_revision: Union[str, None] = "0183"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("design_screens", sa.Column("brief", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("design_screens", "brief")
