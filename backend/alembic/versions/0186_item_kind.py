"""`items.kind`: feature / fix / chore, empty = not recorded (GRPH-1358).

Existing rows stay empty on purpose: an old item reads as "not recorded", never "not a feature".

Revision ID: 0186
Revises: 0185
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0186"
down_revision: Union[str, None] = "0185"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("kind", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("items", "kind")
