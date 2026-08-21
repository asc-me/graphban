"""Whether a held area was predicted or declared (PRD-20 D4, GRPH-387).

Hand-written, as 0077-0081 were.

`held_areas` returned `predicted: False` for every row because there was nowhere to read it
from, so the graph's dashed "guess" channel could never light from the API. `claim_cluster`
knew the answer at write time and dropped it.

Backfilled to `false`, which is the honest value rather than a convenient one: an existing
reservation was written before anything recorded this, so whether its areas were predicted
is genuinely unknown. `false` renders it as a solid claim, and a solid claim that was
actually a guess is the milder of the two errors — a dashed line invented for a hold nobody
recorded would be asserting a distinction we do not have.

Reservations expire on the lease clock (600s), so every row this backfills is gone within
ten minutes and the ambiguity does not outlive the deploy.

Revision ID: 0082
Revises: 0081
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0082"
down_revision: Union[str, None] = "0081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "area_reservations",
        sa.Column("predicted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("area_reservations", "predicted")
