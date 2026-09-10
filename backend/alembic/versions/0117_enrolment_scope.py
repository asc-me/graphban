"""The scope a seat was minted for, so a child cannot claim past its wave (GRPH-827).

`--prd` bounded what the supervisor DELEGATED and nothing else. A worker that finished its
assigned item did what every posture teaches it to do — call `claim_cluster` for the next ready
cluster — and `claim_cluster` had no scope to consult. Measured on one wave scoped to a single
PRD: three delegated items inside it, six self-claimed outside it, one of them an ops item whose
checklist mutates production and which sits top of the queue on score.

On the SEAT rather than on the flag, because the flag lives in a process the child does not run.
A scope the child has to choose to honour is a comment; a scope its credential carries is a
control.

NULL is unscoped, which is what every seat minted before this is, and what an unscoped wave
still mints. The column cannot be filled in later from the wave, because `wave` is a label the
operator types and not an id.

Revision ID: 0117
Revises: 0116
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0117"
down_revision: Union[str, None] = "0116"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrolments", sa.Column("prd_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("enrolments", "prd_id")
