"""Dismiss an agent from the roster without deleting it (GRPH-380).

A roster that only grows is one nobody reads: a day of walking left 24 rows of which 16 were
dead processes holding nothing. Collapsing helped; being able to say "I am done with this one"
is the other half.

DISMISSED, NOT DELETED, and the model comment says why in full: `Item.claimed_by`,
`reviewed_by` and `built_by` hold agent ids as plain strings, so removing a row dangles them
silently — and `keys.mint` allocates `max(number) + 1`, so a freed number would let one id name
two different agents at different times. Everything that made authorship worth preserving in
0067 makes deletion the wrong tool here.

Revision ID: 0068
Revises: 0067
"""
from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agents_dismissed", "agents", ["dismissed_at"])


def downgrade() -> None:
    op.drop_index("ix_agents_dismissed", table_name="agents")
    op.drop_column("agents", "dismissed_at")
