"""proposal pull cursor and exactly-once receipts (GRPH-1737).

Adds the proposal cursor, etag, last-poll time and pull-now flag on
`sync_project`, and `sync_proposal`, the receipt written in the same
transaction as an apply. A re-delivered id finds this row and is not applied
again. `reported` stays false until the console accepts the outcome.

Revision ID: 0212
Revises: 0211
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0212"
down_revision: Union[str, None] = "0211"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sync_project") as batch:
        batch.add_column(sa.Column(
            "proposal_cursor", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("proposal_etag", sa.String(), nullable=True))
        batch.add_column(sa.Column(
            "proposal_polled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column(
            "proposal_pull_now", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "sync_proposal",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("current_version", sa.BigInteger(), nullable=True),
        sa.Column("reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sync_proposal"),
    )
    op.create_index("ix_sync_proposal_project_id", "sync_proposal", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_proposal_project_id", table_name="sync_proposal")
    op.drop_table("sync_proposal")
    with op.batch_alter_table("sync_project") as batch:
        batch.drop_column("proposal_pull_now")
        batch.drop_column("proposal_polled_at")
        batch.drop_column("proposal_etag")
        batch.drop_column("proposal_cursor")
