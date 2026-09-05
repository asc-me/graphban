"""Fleet profiles and project fleet policy (PRD-37 D3, D4).

`fleet_profiles`: one row per user (NULL project = the default) or per user × project (an
override). `projects.fleet_policy`: the project's hard constraints, NULL meaning none. Both
are read by the SUPERVISOR through payloads it already fetches; the server stores and serves
and never resolves.

Revision ID: 0110
Revises: 0109
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0110"
down_revision: Union[str, None] = "0109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fleet_profiles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("defaults", sa.JSON(), nullable=True),
        sa.Column("weights", sa.JSON(), nullable=True),
        sa.Column("excludes", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_fleet_profiles_user_id", "fleet_profiles", ["user_id"])
    op.create_index("ix_fleet_profiles_project_id", "fleet_profiles", ["project_id"])
    op.create_index("ix_fleet_profiles_user_project", "fleet_profiles", ["user_id", "project_id"], unique=True)
    op.add_column("projects", sa.Column("fleet_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "fleet_policy")
    op.drop_index("ix_fleet_profiles_user_project", table_name="fleet_profiles")
    op.drop_index("ix_fleet_profiles_project_id", table_name="fleet_profiles")
    op.drop_index("ix_fleet_profiles_user_id", table_name="fleet_profiles")
    op.drop_table("fleet_profiles")
