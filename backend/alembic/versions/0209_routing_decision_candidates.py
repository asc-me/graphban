"""routing_decision_candidates: `routing_decisions.candidates`, the ranked candidate set a
capacity-routing decision was made from, so replay re-ranks it under a proposed policy (GRPH-1712).

Revision ID: 0209
Revises: 0208
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0209"
down_revision: Union[str, None] = "0208"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("routing_decisions", sa.Column("candidates", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("routing_decisions", "candidates")
