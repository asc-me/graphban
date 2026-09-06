"""The cost proxy's denominator (PRD-38 D11).

`(tokens_in + tokens_out) / signed_off` is the wrong number when only some attempts reported
tokens: a partial numerator over a full denominator makes a vendor that prints nothing look
cheap. The denominator has to be the signed-off attempts **that reported**, and a rollup that
did not keep that count could not produce it — the raw rows it was computed from may be past
retention by the time anyone looks.

Revision ID: 0112
Revises: 0111
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0112"
down_revision: Union[str, None] = "0111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("harness_rollups", sa.Column(
        "signed_off_reported", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("harness_rollups", "signed_off_reported")
