"""Scan finding grain: advisory_id, dependency, fixed_version, kind, count (GRPH-1127).

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
        "scan_findings",
        sa.Column("advisory_id", sa.String(), server_default="", nullable=False),
    )
    op.add_column(
        "scan_findings",
        sa.Column("dependency", sa.String(), server_default="", nullable=False),
    )
    op.add_column(
        "scan_findings",
        sa.Column("fixed_version", sa.String(), server_default="", nullable=False),
    )
    op.add_column(
        "scan_findings",
        sa.Column("kind", sa.String(), server_default="", nullable=False),
    )
    op.add_column("scan_findings", sa.Column("count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("scan_findings", "count")
    op.drop_column("scan_findings", "kind")
    op.drop_column("scan_findings", "fixed_version")
    op.drop_column("scan_findings", "dependency")
    op.drop_column("scan_findings", "advisory_id")
