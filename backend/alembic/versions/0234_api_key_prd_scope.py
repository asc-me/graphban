"""An API key can be limited to one PRD (GRPH-1973): api_keys.prd_id.

Every existing key stays unlimited: prd_id NULL.

Revision ID: 0234
Revises: 0233
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0234"
down_revision: Union[str, None] = "0233"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("prd_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "prd_id")
