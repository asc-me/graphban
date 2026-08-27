"""The code map can say which commit it describes (GRPH-54).

`content_hash` already answers "has this ONE file changed since I described it" — but only
for a file the agent still has in hand to re-hash. It cannot answer the question an agent
actually asks before trusting the map: "is this projection current for the tree I am looking
at?" A per-node hash gives no map-level answer, and a map with no revision reads exactly like
a map that is up to date.

`revision` is the commit the node was described AT, supplied by the describing agent — it has
the repo checked out, so it is the only party that knows. Empty means an older node described
before this existed, or an agent that did not pass one; that is deliberately distinguishable
from a known revision, because "unknown" and "current" must not collapse.

Revision ID: 0089
Revises: 0088
"""
import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so the column is non-null on rows that already exist. Every node
    # described before this migration has an unknown revision, which is the truthful value —
    # backfilling them with the current HEAD would assert something nobody verified.
    op.add_column(
        "code_nodes",
        sa.Column("revision", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("code_nodes", "revision")
