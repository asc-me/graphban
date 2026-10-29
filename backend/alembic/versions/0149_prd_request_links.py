"""`prd_request_links`: many-to-many PRD <-> request (PRD-60 D10 / GRPH-1140).

Revision ID: 0149
Revises: 0148
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0149"
down_revision: Union[str, None] = "0148"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prd_request_links",
        sa.Column("prd_id", sa.String(), sa.ForeignKey("prds.id"), primary_key=True),
        sa.Column("request_id", sa.String(), sa.ForeignKey("requests.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("prd_request_links")
