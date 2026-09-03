"""Mark projects whose legacy provider blob the boot migration has consumed.

The migration's only idempotency marker was the pointer it wrote. Removing an
override rule in the credentials console sets that pointer back to None —
indistinguishable from "never migrated" — so the next boot re-pointed the
project from the blob that is deliberately still on disk. The rule the
operator deleted resurrected; the reference deployment's boot log showed
`projects_pointed: 2` after a restart on a box nobody had configured.

The flag is written when the pointer is and by any explicit pointer edit in
the console, and it outlives the clear, so a removed rule stays removed.

Revision ID: 0101
Revises: 0100
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0101"
down_revision: Union[str, None] = "0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "credential_migrated", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "credential_migrated")
