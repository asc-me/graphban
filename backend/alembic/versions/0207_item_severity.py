"""item_severity: `items.severity` (GRPH-1683).

Revision ID: 0207
Revises: 0206
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0207"
down_revision: Union[str, None] = "0206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("severity", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("items", "severity")
