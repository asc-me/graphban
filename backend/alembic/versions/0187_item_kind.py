"""`items.kind`: feature / fix / chore, empty = not recorded (GRPH-1358).

Existing rows are left EMPTY on purpose. Nothing recorded which past changes were
features, and backfilling a guess would put a confident wrong answer into release notes;
an empty kind reads as "not recorded", which is the truth.

Revision ID: 0187
Revises: 0186
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0187"
down_revision: Union[str, None] = "0186"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("kind", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("items", "kind")
