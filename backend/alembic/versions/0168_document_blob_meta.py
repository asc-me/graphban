"""Document blob mime type and byte length (GRPH-1276 / GRPH-P64).

Revision ID: 0168
Revises: 0167
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0168"
down_revision: Union[str, None] = "0167"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("blob_mime", sa.String(), server_default="", nullable=False))
    op.add_column("documents", sa.Column("blob_size", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("documents", "blob_size")
    op.drop_column("documents", "blob_mime")
