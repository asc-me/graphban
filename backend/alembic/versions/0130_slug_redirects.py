"""PRD-43 D8: slug redirect table for upgrade 301s.

Revision ID: 0130
Revises: 0129
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0130"
down_revision: Union[str, None] = "0129"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "slug_redirects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("old_host", sa.String(), nullable=False),
        sa.Column("new_host", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_slug_redirects_org_id", "slug_redirects", ["org_id"])
    op.create_unique_constraint(
        "uq_slug_redirects_old_host", "slug_redirects", ["old_host"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_slug_redirects_old_host", "slug_redirects", type_="unique")
    op.drop_index("ix_slug_redirects_org_id", table_name="slug_redirects")
    op.drop_table("slug_redirects")
