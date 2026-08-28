"""An item remembers when its PR was linked (GRPH-567).

PRD-26 §PR cooldown: a minimum interval between linking a PR and recording its outcome, so
CI has time to run. It kills the failure class where `done` is written before the run it
claims to rest on has finished — or started.

The interval cannot be enforced without a time to measure from, and evidence rows carry no
timestamp: they hold `kind`, `detail`, `url`, `schema_version` and their kind-specific
fields, and nothing says when any of them arrived. `updated_at` cannot stand in — it moves
for every write, so linking a PR and then editing the description would reset the clock.

NULL means no PR has been linked, which is the truthful value for every existing row and for
every item that never had one. Backfilling from `created_at` or `updated_at` would assert a
link nobody made and would put historical items into a cooldown they never entered; the same
reasoning 0089 and 0090 give for `revision` and `head_commit`.
"""
import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("pr_linked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "pr_linked_at")
