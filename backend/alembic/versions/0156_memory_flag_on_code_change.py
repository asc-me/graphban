"""`projects.memory_flag_on_code_change`: flag published memory when its cited code changes (GRPH-1213).

Revision ID: 0156
Revises: 0155
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0156"
down_revision: Union[str, None] = "0155"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("memory_flag_on_code_change", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("projects", "memory_flag_on_code_change")
