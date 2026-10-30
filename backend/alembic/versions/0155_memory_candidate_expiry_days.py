"""`projects.memory_candidate_expiry_days`: days before an uncorroborated candidate expires (GRPH-1212).

Revision ID: 0155
Revises: 0154
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0155"
down_revision: Union[str, None] = "0154"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("memory_candidate_expiry_days", sa.Integer(), nullable=False, server_default="45"),
    )


def downgrade() -> None:
    op.drop_column("projects", "memory_candidate_expiry_days")
