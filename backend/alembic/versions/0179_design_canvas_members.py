"""`design_canvases.members` and `root`: a zip canvas records its files; `design_findings.member`
and `member_hash`: a finding can name the member it is about (GRPH-1306).

Revision ID: 0179
Revises: 0178
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0179"
down_revision: Union[str, None] = "0178"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("design_canvases") as batch:
        batch.add_column(sa.Column("members", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("root", sa.String(), nullable=False, server_default=""))
    with op.batch_alter_table("design_findings") as batch:
        batch.add_column(sa.Column("member", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("member_hash", sa.String(), nullable=False,
                                   server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("design_findings") as batch:
        batch.drop_column("member_hash")
        batch.drop_column("member")
    with op.batch_alter_table("design_canvases") as batch:
        batch.drop_column("root")
        batch.drop_column("members")
