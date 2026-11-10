"""documents.item_id — a reference document's optional item link (GRPH-1495).

Revision ID: 0193
Revises: 0192
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0193"
down_revision: Union[str, None] = "0192"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("item_id", sa.String(), nullable=True))
    op.create_index("ix_documents_item_id", "documents", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_item_id", table_name="documents")
    op.drop_column("documents", "item_id")
