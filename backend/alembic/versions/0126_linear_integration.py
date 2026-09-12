"""Linear integration adapter (PRD-P10).

Per-org Linear OAuth link: encrypted access token, webhook secret, workspace identity,
and freshness timestamps. One row per org ↔ Linear workspace.

Revision ID: 0126
Revises: 0125
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0126"
down_revision: Union[str, None] = "0125"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "linear_integrations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("access_token_enc", sa.String(), nullable=False, server_default=""),
        sa.Column("webhook_secret_enc", sa.String(), nullable=False, server_default=""),
        sa.Column("workspace_id", sa.String(), nullable=False, server_default=""),
        sa.Column("workspace_name", sa.String(), nullable=False, server_default=""),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("linear_integrations")
