"""`design_findings.canvas_check`: text a canvas must hold for a finding to be applied (GRPH-1305).

Revision ID: 0178
Revises: 0177
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0178"
down_revision: Union[str, None] = "0177"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("design_findings") as batch:
        batch.add_column(sa.Column("canvas_check", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("design_findings") as batch:
        batch.drop_column("canvas_check")
