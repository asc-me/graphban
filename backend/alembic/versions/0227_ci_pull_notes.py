"""CI pull sentences, readable by every worker (GRPH-1822).

The pull loop runs in the leader. `/health` and the done refusal run on
whichever worker the request hits, so the sentence cannot live in that
process.

Revision ID: 0227
Revises: 0226
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0227"
down_revision: Union[str, None] = "0226"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ci_pull_notes",
        sa.Column("key", sa.String(length=300), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ci_pull_notes")
