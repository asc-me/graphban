"""The posture a credential was minted for (GRPH-362 / PRD-17).

Found during the D-h acceptance walk: an `all-in-one` credential connected to Grok, and the
roster showed the agent as `worker`. The fleet role prompt had passed `role_hint="worker"`,
`register_agent` honoured it because the key permits that role, and the agent was gated as a
worker — losing `sign_off` and the ability to write `done`.

That contradicts `register_agent`'s own stated invariant, "registering must never cost an agent
capability it already had", which until now only held in the branch where no hint was given.

The root cause is representational: an all-in-one mint writes `["planner","worker","reviewer"]`,
byte-identical to a key that never set roles at all. The credential could not express that
all-in-one was CHOSEN, so an unverified string from a client config outranked a posture the
human picked in the UI.

NULL is the migration position and keeps every existing key behaving exactly as it does today —
legacy keys and shared fleet keys still honour hints, which is what a fleet running off one
credential depends on.

Revision ID: 0065
Revises: 0064
"""
from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("posture", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "posture")
