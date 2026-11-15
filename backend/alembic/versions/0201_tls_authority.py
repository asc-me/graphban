"""tls_authority + tls_names: the box's own certificate authority and name allowlist (GRPH-1634).

Revision ID: 0201
Revises: 0200
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0201"
down_revision: Union[str, None] = "0200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tls_authority",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ca_cert", sa.Text(), nullable=False),
        sa.Column("ca_key", sa.Text(), nullable=False),
        sa.Column("ca_not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leaf_cert", sa.Text(), nullable=False, server_default=""),
        sa.Column("leaf_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("leaf_names", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("leaf_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leaf_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tls_names",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tls_names")
    op.drop_table("tls_authority")
