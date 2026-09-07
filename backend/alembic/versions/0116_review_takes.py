"""How many times an item's review has been TAKEN without a verdict (GRPH-771).

Measured on the deployed instance: GRPH-A142 called `claim_review` every 50 seconds and
nothing else. Each call reset `review_claimed_at`, so a 600-second lease never came within 550
seconds of lapsing and the item was never offered to anyone else. Fixing the clock stops the
hold being unbounded, but a loop that re-takes the item every time it lapses still looks, on
any single read, exactly like a reviewer who started work a moment ago.

The count is the fact that separates them, and it is one integer. "Taken 7 times, no verdict"
is a diagnosis; "held for 12 seconds" is what that same situation looked like before.

Cleared at sign-off and at bounce — a verdict is what the count is counting the absence of, so
a decided item carries no arrears into whatever happens to it next.

Revision ID: 0116
Revises: 0115
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0116"
down_revision: Union[str, None] = "0115"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("review_takes", sa.Integer(), nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    op.drop_column("items", "review_takes")
