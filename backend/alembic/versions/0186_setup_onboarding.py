"""Guided setup state (GRPH-1360, PRD-P67 §6).

`users.onboarding` — UI progress only (quick-start finished/dismissed, tour). NULL is "never
onboarded", which is what triggers the quick-start dialog, so every user that exists when this
runs is backfilled as finished (G5): an existing account must not see the dialog pop.

`projects.setup_decisions` — the only facts the setup checklist stores rather than reads: the
human decisions (P1 workspace confirm, "stay offline", the step ⓪ answer). NULL is "nobody
decided anything yet".

Revision ID: 0186
Revises: 0185
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0186"
down_revision: Union[str, None] = "0185"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("onboarding", sa.JSON(), nullable=True))
    op.add_column("projects", sa.Column("setup_decisions", sa.JSON(), nullable=True))
    # Backfill: every existing account is treated as having finished the quick start. The
    # timestamp is the migration time — the honest answer to "when did this become true".
    users = sa.table("users", sa.column("onboarding", sa.JSON()))
    stamp = datetime.now(timezone.utc).isoformat()
    op.get_bind().execute(
        users.update()
        .where(users.c.onboarding.is_(None))
        .values(onboarding={"quickstart_finished_at": stamp, "backfilled": True})
    )


def downgrade() -> None:
    op.drop_column("projects", "setup_decisions")
    op.drop_column("users", "onboarding")
