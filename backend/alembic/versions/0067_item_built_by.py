"""Authorship outlives the lease (GRPH-377 / GRPH-376).

`claimed_by` was carrying two facts: WHO HOLDS THIS NOW and WHO MADE IT. Every path that
correctly releases a lease therefore destroyed the authorship with it:

- `sign_off` cleared it, so a done item read `built_by: -` and the self-review ban became
  unprovable after the fact (GRPH-376);
- `end_wave` cleared it on items still in REVIEW — it only resets status for `in_progress` —
  so `independent(reviewer, None)` read "nothing to be independent of" and an agent could sign
  off work it had built itself, provided a wave ended in between (GRPH-377).

The second was found on the PRD-17 walk while setting up step 9, and is the sharper of the
two: an enforcement input removed by a routine operation, while the work was still in flight.

`built_by` is written at claim and never cleared. `claimed_by` stays exactly what it always
was.

BACKFILL: `claimed_by` where a lease is still held. Authorship already erased by a sign_off or
an End wave is UNRECOVERABLE and is left empty rather than guessed — an item whose author we
cannot name should say so, not name the wrong agent.

Revision ID: 0067
Revises: 0066
"""
from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("built_by", sa.String(), nullable=True))
    op.create_index("ix_items_built_by", "items", ["built_by"])
    # Only where a lease survives. Everything else is genuinely unknown.
    op.execute("UPDATE items SET built_by = claimed_by WHERE claimed_by IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_items_built_by", table_name="items")
    op.drop_column("items", "built_by")
