"""An orphaned branch is DERIVED, not written by one code path (GRPH-396).

`agents.branch_orphaned` was written in exactly one place — inside `quarantine()`, which its own
docstring describes as "only ever reached by an agent that is demonstrably alive". So the flag
fired for the DRIFTING agent and never for the DEAD one, which is the common case and the one
the flag exists for: a crashed agent is precisely the agent that cannot clean up after itself.

It also silently disabled the guard built on top of it. Dismissing an agent refuses when its
branch is orphaned, so unfinished business cannot be hidden — but for a dead agent the flag was
never set, so Dismiss went straight through on exactly the rows an operator dismisses most.

The fact is derivable and has one true definition: an agent that declared a branch and is no
longer here left it behind. Computed on read, like presence, seat state and `live_waves` —
which is also why a second writer must not exist for it.

Revision ID: 0072
Revises: 0071
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import false

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agents", "branch_orphaned")


def downgrade() -> None:
    op.add_column("agents", sa.Column("branch_orphaned", sa.Boolean(), nullable=False,
                                      server_default=false()))
