"""`routing_decisions` and `entitlements.next_reset_at`: capacity routing (GRPH-1250).

Revision ID: 0164
Revises: 0163
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0164"
down_revision: Union[str, None] = "0163"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("entitlements", sa.Column("next_reset_at", sa.DateTime(timezone=True),
                                            nullable=True))
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("entitlement_id", sa.String(), sa.ForeignKey("entitlements.id"),
                  nullable=True),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("agent_vendor", sa.String(), nullable=False, server_default=""),
        sa.Column("agent_model", sa.String(), nullable=False, server_default=""),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_routing_decisions_item_id", "routing_decisions", ["item_id"])
    op.create_index("ix_routing_decisions_entitlement_id", "routing_decisions",
                    ["entitlement_id"])


def downgrade() -> None:
    op.drop_index("ix_routing_decisions_entitlement_id", table_name="routing_decisions")
    op.drop_index("ix_routing_decisions_item_id", table_name="routing_decisions")
    op.drop_table("routing_decisions")
    op.drop_column("entitlements", "next_reset_at")
