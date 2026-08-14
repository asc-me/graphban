"""The bounce reason is kept (GRPH-378).

`bounce()` has always required a non-empty reason and then discarded it: no column held it,
the event meta recorded only the principal, and on the live fleet DB the string appeared in no
row of any table after a real bounce. The author got the item back with nothing to act on —
which is precisely the failure the requirement was written to prevent.

Found on the PRD-17 acceptance walk, step 9, driven over the real MCP surface. 1768 tests were
green: `test_a_bounce_needs_a_reason` asserts the REFUSAL on a blank reason, and nothing
asserted the reason was ever readable.

No backfill is possible — every reason given before this migration is gone.

Revision ID: 0069
Revises: 0068
"""
from alembic import op
import sqlalchemy as sa

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("bounce_reason", sa.String(), nullable=False,
                                     server_default=""))


def downgrade() -> None:
    op.drop_column("items", "bounce_reason")
