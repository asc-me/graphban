"""`users.onboarding` and `projects.setup_decisions` for guided setup (GRPH-1360).

Existing users are backfilled as having finished the quick start (they are not new); a user
created after this migration has `onboarding` NULL.

Revision ID: 0186
Revises: 0185
"""

import json
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
    done = json.dumps({"quickstart_finished_at": datetime.now(timezone.utc).isoformat()})
    op.get_bind().execute(sa.text("UPDATE users SET onboarding = CAST(:v AS JSON)"), {"v": done})


def downgrade() -> None:
    op.drop_column("projects", "setup_decisions")
    op.drop_column("users", "onboarding")
