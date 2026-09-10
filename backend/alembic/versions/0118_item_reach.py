"""What an item's work reaches: this repository, or something deployed (GRPH-832).

An item's touchpoints are a claim about FILES. On 2026-09-08 a worker was handed an item whose
touchpoints were four ordinary repository files and whose description implied production; it
rotated the production encryption key, deleted rows, set production environment variables and
redeployed, and modified none of the four. There was no way to declare that an item reaches
outside the repository at all, so that item was structurally identical to a docs change.

`repo` for everything that exists, because that is what almost all work is and because a
default of "unknown" would make every item on every deployment undelegatable on the morning
this lands. The safety this buys is therefore opt-in per item — stated plainly here rather
than discovered later, and it is why the prose signals exist beside it.

`deploy` is settable only through `PATCH /api/items/{id}`, which takes a bearer JWT. That is
the boundary and not a convention: no agent credential reaches that route.

Revision ID: 0118
Revises: 0117
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0118"
down_revision: Union[str, None] = "0117"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("reach", sa.String(), nullable=False,
                                     server_default="repo"))
    op.add_column("delegations", sa.Column("reach_acknowledged", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("delegations", "reach_acknowledged")
    op.drop_column("items", "reach")
