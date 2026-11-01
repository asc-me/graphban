"""`platform_observation_rollups`: opt-in observation counts by kind (GRPH-1286, PRD-P63 D8).

Revision ID: 0174
Revises: 0173
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0174"
down_revision: Union[str, None] = "0173"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_observation_rollups",
        sa.Column("week", sa.String(8), primary_key=True),
        sa.Column("kind", sa.String(40), primary_key=True),
        sa.Column("orgs_contributing", sa.Integer(), nullable=False),
        sa.Column("top_org_share", sa.Float(), nullable=True),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.Column("rolled_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_observation_rollups")
