"""Opt-in bound_only on a seat (GRPH-1089).

A seat bound to one item (`delegate(seat=true)`) claims and builds that item, then carries
on into `claim_review` and reviews other agents' items. That is what a `gbfleet until` wave
relies on, so it stays the default. `bound_only` is the opt-in that stops it: once the bound
item has left `in_progress`, `claim_review` and `claim_next` refuse and name the binding.

On the SEAT rather than in the child's prompt, for the reason `review_only` (0135) gives: a
limit the child has to choose to honour is a comment. False is the default and is what every
seat minted before this column was, so an existing wave is unaffected.

Revision ID: 0139
Revises: 0138
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0139"
down_revision: Union[str, None] = "0138"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrolments", sa.Column("bound_only", sa.Boolean(), nullable=False,
                                          server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("enrolments", "bound_only")
