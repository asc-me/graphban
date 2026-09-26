"""PRD-45 S2: decider credential pointers.

Adds `decider_credential_id` to both `deployment_config` (the scope default) and
`projects` (per-project override), mirroring the chat/embedding pointer shape.

Revision ID: 0131
Revises: 0130
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0131"
down_revision: Union[str, None] = "0130"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deployment_config",
        sa.Column("decider_credential_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_deployment_config_decider_credential_id",
        "deployment_config",
        "credentials",
        ["decider_credential_id"],
        ["id"],
    )
    op.add_column(
        "projects",
        sa.Column("decider_credential_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_decider_credential_id",
        "projects",
        "credentials",
        ["decider_credential_id"],
        ["id"],
    )
    op.create_index(
        "ix_projects_decider_credential_id",
        "projects",
        ["decider_credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_decider_credential_id", table_name="projects")
    op.drop_constraint("fk_projects_decider_credential_id", "projects", type_="foreignkey")
    op.drop_column("projects", "decider_credential_id")
    op.drop_constraint(
        "fk_deployment_config_decider_credential_id",
        "deployment_config",
        type_="foreignkey",
    )
    op.drop_column("deployment_config", "decider_credential_id")
