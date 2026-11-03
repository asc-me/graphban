"""`auto_update_config.timezone_set`: has a PATCH ever written the zone (GRPH-1302).

The panel pre-selects the browser's zone until the owner saves one. "UTC" is both the
untouched default and a real choice, so the value alone cannot tell them apart.

Revision ID: 0176
Revises: 0175
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0176"
down_revision: Union[str, None] = "0175"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("auto_update_config") as batch:
        batch.add_column(sa.Column("timezone_set", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("auto_update_config") as batch:
        batch.drop_column("timezone_set")
