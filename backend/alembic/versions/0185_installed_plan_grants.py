"""`installed_plan_grants`: the signed plan grant a self-hosted box runs on (GRPH-1343).

Revision ID: 0185
Revises: 0184
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0185"
down_revision: Union[str, None] = "0184"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "installed_plan_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("deployment_id", sa.String(), nullable=False, server_default=""),
        sa.Column("plan", sa.String(), nullable=False),
        sa.Column("issued_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column("installed_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("installed_plan_grants")
