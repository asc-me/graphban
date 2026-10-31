"""`entitlements` and `agents.entitlement_ids`: the capacity-routing registry (GRPH-1248).

The CHECK is the structural half of PRD-62 D2: a `subscription` (or `local`) row cannot hold a
`secret_ref`, whatever the API forgets to refuse.

Revision ID: 0162
Revises: 0161
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0162"
down_revision: Union[str, None] = "0161"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "entitlements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("vendor", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("runner", sa.String(), nullable=False, server_default=""),
        sa.Column("plan_label", sa.String(), nullable=False, server_default=""),
        sa.Column("reset_cadence", sa.String(), nullable=False, server_default=""),
        sa.Column("secret_ref", sa.String(), nullable=True),
        sa.Column("token_budget", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("secret_ref IS NULL OR kind IN ('api_key', 'cloud')",
                           name="ck_entitlements_secret_only_api_key_cloud"),
    )
    op.create_index("ix_entitlements_org_id", "entitlements", ["org_id"])
    op.create_index("ix_entitlements_owner_user_id", "entitlements", ["owner_user_id"])
    op.add_column("agents", sa.Column("entitlement_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "entitlement_ids")
    op.drop_index("ix_entitlements_owner_user_id", table_name="entitlements")
    op.drop_index("ix_entitlements_org_id", table_name="entitlements")
    op.drop_table("entitlements")
