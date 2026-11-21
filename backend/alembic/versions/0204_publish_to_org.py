"""publish_to_org: per-project switches for publishing specs and item digests (GRPH-1674).

Both on by default, including for existing projects: the server_default backfills them.

Revision ID: 0204
Revises: 0203
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0204"
down_revision: Union[str, None] = "0203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in ("publish_specs", "publish_item_digests"):
        op.add_column("projects", sa.Column(col, sa.Boolean(), nullable=False,
                                            server_default=sa.true()))


def downgrade() -> None:
    for col in ("publish_item_digests", "publish_specs"):
        op.drop_column("projects", col)
