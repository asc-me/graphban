"""`users.onboarding` and `projects.setup_decisions` for guided setup (GRPH-1360).

Existing users are backfilled as having finished the quick start (they are not new); a user
created after this migration has `onboarding` NULL.

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
    backfill(op.get_bind())


def backfill(conn) -> None:
    """Every user who exists now is not new: mark the quick start finished (GRPH-P67 G5).
    A typed JSON column, so the value binds as JSON on Postgres and SQLite alike."""
    users = sa.table("users", sa.column("onboarding", sa.JSON()))
    done = {"quickstart_finished_at": datetime.now(timezone.utc).isoformat()}
    conn.execute(sa.update(users).where(users.c.onboarding.is_(None)).values(onboarding=done))


def downgrade() -> None:
    op.drop_column("projects", "setup_decisions")
    op.drop_column("users", "onboarding")
