"""`machines` — dev machines that reported from `gban setup` / `gban doctor` (GRPH-1442).

Revision ID: 0190
Revises: 0189
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0190"
down_revision: Union[str, None] = "0189"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("machine_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("os", sa.String(), nullable=False, server_default=""),
        sa.Column("gban_version", sa.String(), nullable=False, server_default=""),
        sa.Column("gbfleet_version", sa.String(), nullable=False, server_default=""),
        sa.Column("harnesses", sa.JSON(), nullable=True),
        sa.Column("projects", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="clean"),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "machine_id", name="uq_machines_user_machine"),
    )
    op.create_index("ix_machines_user_id", "machines", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_machines_user_id", table_name="machines")
    op.drop_table("machines")
