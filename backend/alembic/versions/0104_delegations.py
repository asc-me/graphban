"""Delegation as a ledger fact (PRD-35).

One row per delegation, written by `delegate` before the child exists, linked when a
declared child claims the item, closed when the owner withdraws or a stranger claims, and
finished at the item transition that ended the attempt. `expired` — open past its own
`lease_seconds` — is the state nothing could show before: a spawn that never arrived.

Revision ID: 0104
Revises: 0103
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0104"
down_revision: Union[str, None] = "0103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("item_id", sa.String(), sa.ForeignKey("items.id"), nullable=False),
        sa.Column("delegated_by", sa.String(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("linked_by", sa.String(length=16), nullable=True),
        sa.Column("lane", sa.String(length=16), nullable=False),
        sa.Column("requested_tier", sa.String(length=16), nullable=False),
        sa.Column("declared_model", sa.String(length=64), nullable=True),
        sa.Column("declared_tier", sa.String(length=16), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("closed_reason", sa.String(length=16), nullable=True),
        sa.Column("closed_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_seconds", sa.Integer(), nullable=False, server_default="600"),
    )
    op.create_index("ix_delegations_project_id", "delegations", ["project_id"])
    op.create_index("ix_delegations_item_id", "delegations", ["item_id"])
    op.create_index("ix_delegations_delegated_by", "delegations", ["delegated_by"])
    op.create_index("ix_delegations_item_created", "delegations", ["item_id", "created_at"])
    op.create_index("ix_delegations_delegator_created", "delegations",
                    ["delegated_by", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_delegations_delegator_created", table_name="delegations")
    op.drop_index("ix_delegations_item_created", table_name="delegations")
    op.drop_index("ix_delegations_delegated_by", table_name="delegations")
    op.drop_index("ix_delegations_item_id", table_name="delegations")
    op.drop_index("ix_delegations_project_id", table_name="delegations")
    op.drop_table("delegations")
