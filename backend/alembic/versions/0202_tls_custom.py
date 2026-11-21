"""tls_custom: an uploaded or Let's Encrypt certificate and the redirect switch (GRPH-1637).

Revision ID: 0202
Revises: 0201
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0202"
down_revision: Union[str, None] = "0201"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tls_custom",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("box_address", sa.String(), nullable=False),
        sa.Column("cert_chain", sa.Text(), nullable=False, server_default=""),
        sa.Column("cert_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("subject", sa.String(), nullable=False, server_default=""),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("warnings", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("redirect_on", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tls_custom")
