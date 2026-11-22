"""Producer marker on code nodes (GRPH-1845).

Records which code map producer last stamped a node's revision and content_hash, so a
describe from a branch cannot move them. Server default "" = never stamped; no backfill.

Revision ID: 0226
Revises: 0225
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0226"
down_revision: Union[str, None] = "0225"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("code_nodes") as b:
        b.add_column(sa.Column("producer", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("code_nodes") as b:
        b.drop_column("producer")
