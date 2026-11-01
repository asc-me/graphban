"""CodeScan.result — producer outcome (GRPH-1125 / PRD-P58).

Revision ID: 0142
Revises: 0141
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0142"
down_revision: Union[str, None] = "0141"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "code_scans",
        sa.Column("result", sa.String(), server_default="complete", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("code_scans", "result")
