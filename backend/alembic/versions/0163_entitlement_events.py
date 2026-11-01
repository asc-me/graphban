"""`entitlement_events`: the per-entitlement headroom ledger (GRPH-1249).

Revision ID: 0163
Revises: 0162
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0163"
down_revision: Union[str, None] = "0162"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "entitlement_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entitlement_id", sa.String(), sa.ForeignKey("entitlements.id"),
                  nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("vendor_text", sa.String(), nullable=False, server_default=""),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_entitlement_events_entitlement_id", "entitlement_events",
                    ["entitlement_id"])


def downgrade() -> None:
    op.drop_index("ix_entitlement_events_entitlement_id", table_name="entitlement_events")
    op.drop_table("entitlement_events")
