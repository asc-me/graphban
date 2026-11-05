"""`installed_rule_packs`: the signed rule pack a person accepted, persisted (GRPH-1329).

Revision ID: 0182
Revises: 0181
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0182"
down_revision: Union[str, None] = "0181"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "installed_rule_packs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("artifact", sa.LargeBinary(), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column("installed_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("installed_rule_packs")
