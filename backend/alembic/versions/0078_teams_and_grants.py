"""Teams, their grants, and where a membership came from (PRD-21 D5).

Hand-written, for the reason 0077 was: `--autogenerate` sweeps in unrelated drift between
the models and the chain in places this change does not touch.

`memberships.origin` defaults to `direct` for every existing row, which is true — every
membership that exists today was written by a human, and none of them may be recomputed
or refused by D8's direct-edit guard.

Revision ID: 0078
Revises: 0077
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0078"
down_revision: Union[str, None] = "0077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_team_name"),
    )
    op.create_index("ix_teams_org_id", "teams", ["org_id"])

    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("team_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_member"),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"])
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])

    op.create_table(
        "team_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("team_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("access", sa.String(), nullable=False, server_default="read"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "project_id", name="uq_team_grant"),
    )
    op.create_index("ix_team_grants_team_id", "team_grants", ["team_id"])
    op.create_index("ix_team_grants_project_id", "team_grants", ["project_id"])

    op.add_column(
        "memberships",
        sa.Column("origin", sa.String(), nullable=False, server_default="direct"),
    )


def downgrade() -> None:
    op.drop_column("memberships", "origin")
    op.drop_index("ix_team_grants_project_id", table_name="team_grants")
    op.drop_index("ix_team_grants_team_id", table_name="team_grants")
    op.drop_table("team_grants")
    op.drop_index("ix_team_members_user_id", table_name="team_members")
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("ix_teams_org_id", table_name="teams")
    op.drop_table("teams")
