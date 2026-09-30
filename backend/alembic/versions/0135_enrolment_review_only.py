"""A seat kind that may review but never take build work (GRPH-988).

A reviewer supervisor run at `--max-workers 0` minted its children plain WORKER seats,
because the review branch in `until.py` typed `role="worker"` and `reviewer` is no longer a
role the server has (S3 / PRD-39 merged it). A worker seat can `claim_next`, so the child
spawned to drain the review queue claimed an unclaimed build item and built it — on a wave
whose operator had said, in the flag they typed, that it would build nothing. The item's own
builder sat idle on another branch and produced nothing.

On the SEAT rather than in the child's prompt, for the reason `prd_id` (0117) gives: a limit
the child has to choose to honour is a comment, and a limit its credential carries is a
control. Not on `role`, because re-adding `reviewer` would undo S3 — a worker builds AND
reviews, and the self-review ban is keyed on authorship, not on a job title. What is being
removed here is one POWER, so it is one flag.

False is the default and is what every seat minted before this column was, so an existing
wave, a human's own agent and a fleet key with no enrolment are all unaffected.

Revision ID: 0135
Revises: 0134
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0135"
down_revision: Union[str, None] = "0134"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrolments", sa.Column("review_only", sa.Boolean(), nullable=False,
                                          server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("enrolments", "review_only")
