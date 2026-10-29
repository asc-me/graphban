"""`items.decision` and `items.snoozed_until`: structured decision waits (PRD-60 D7 / GRPH-1137).

`decision` is a JSON object on a `wait:decision` item holding the agent's question, its
options and, once a person answers, the answer. `snoozed_until` hides a blocker from triage.
Both are nullable and need no backfill: no existing item has either.

Revision ID: 0146
Revises: 0145
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0146"
down_revision: Union[str, None] = "0145"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("decision", sa.JSON(), nullable=True))
    op.add_column("items", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "snoozed_until")
    op.drop_column("items", "decision")
