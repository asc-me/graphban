"""org_utilization_reports — supervisor-reported capacity utilization (PRD-62 S5, GRPH-1480).

Revision ID: 0192
Revises: 0191
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0192"
down_revision: Union[str, None] = "0191"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_utilization_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_org_utilization_reports_org_kind",
        "org_utilization_reports",
        ["org_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_org_utilization_reports_org_kind", table_name="org_utilization_reports")
    op.drop_table("org_utilization_reports")
