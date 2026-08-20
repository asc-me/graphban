"""Where a linked deployment says it can be reached (PRD-21 D6).

Hand-written, as 0077 and 0078 were: `--autogenerate` sweeps in unrelated drift between
the models and the chain in places this change does not touch.

Empty for every existing key, which is honest — no deployment has reported an address yet,
and the console shows that as "not reported" rather than inventing one.

Revision ID: 0079
Revises: 0078
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0079"
down_revision: Union[str, None] = "0078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("base_url", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "base_url")
