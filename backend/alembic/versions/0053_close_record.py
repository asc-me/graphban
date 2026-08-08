"""The terminal state: closed, with the record of what was decided (GRPH-244 / PRD-12).

`Prd.status` was `draft|review|approved`, and `approved` is set before work starts and
never changes — so there was no event to hang an acceptance check on. `closed` is that
event, and `close_record` is what it produced.

The open question this section carried from v1.0 — what state a PRD lands in on a negative
verdict — was answered in the v1.2 rebaseline by dissolving it: the PRD does not leave the
terminal state, the WORK does. Close gates on **disposition** of undelivered intent, never
on delivery. Every baselined section with nothing delivered must first be promoted (to a
backlog item or a successor PRD) or explicitly deferred with a reason. Close is permitted
at zero undispositioned, which is the grill's completion standard one level up.

Stored as JSON on the PRD rather than as a table because a PRD closes once and never
reopens; a second row could only ever disagree with the first. Non-null means closed, so
there is no separate flag able to drift out of step with it.

Revision ID: 0053
Revises: 0052
"""
from alembic import op
import sqlalchemy as sa

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prds", sa.Column("close_record", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("prds", "close_record")
