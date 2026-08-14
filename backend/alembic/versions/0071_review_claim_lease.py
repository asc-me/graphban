"""A review claim is a lease with an expiry, and stops overloading `reviewed_by` (GRPH-395).

`claim_review` leased an item by writing `reviewed_by`, and `sign_off` wrote the SAME column to
record the verdict. One column, two meanings — the exact shape that produced GRPH-376/377, where
releasing a lease destroyed the authorship stored beside it.

Here it produced a different failure: nothing ever cleared the claim. No expiry, no sweep, and
`release_item` refuses a reviewer because a reviewer holds no `claimed_by`. A reviewer that died
mid-review kept the item out of every other reviewer's candidate list forever, while it sat in
`review` looking like ordinary queued work. Found on the walk: FA-9, reviewer silent 2333s.

`review_claimed_by` / `review_claimed_at` are the HOLD, and expire like every other lease.
`reviewed_by` goes back to meaning exactly one thing: who signed it off.

BACKFILL: an in-flight claim under the old semantics is one where the item is still in `review`.
It moves to the new columns with a NULL `review_claimed_at`, which reads as already expired — so
every item stranded by this bug is claimable again the moment this lands, which is the point. A
`done` item's `reviewed_by` is a verdict and is left exactly where it is.

Revision ID: 0071
Revises: 0070
"""
from alembic import op
import sqlalchemy as sa

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("review_claimed_by", sa.String(), nullable=True))
    op.add_column("items", sa.Column("review_claimed_at", sa.DateTime(timezone=True),
                                     nullable=True))
    op.create_index("ix_items_review_claimed_by", "items", ["review_claimed_by"])
    # Only items still in review held a CLAIM; anywhere else the column is a verdict.
    op.execute("""
        UPDATE items SET review_claimed_by = reviewed_by, reviewed_by = NULL
        WHERE status = 'review' AND reviewed_by IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE items SET reviewed_by = review_claimed_by
        WHERE status = 'review' AND review_claimed_by IS NOT NULL
    """)
    op.drop_index("ix_items_review_claimed_by", table_name="items")
    op.drop_column("items", "review_claimed_at")
    op.drop_column("items", "review_claimed_by")
