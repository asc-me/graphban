"""sync_link.deployment_id: the deployment the held token belongs to (GRPH-1576, GBC-88).

The console's approved poll carries `deployment_id`; the box keeps it and sends it as
`previous_deployment_id` on the next device-link start, so approving a re-sign-in revokes
exactly the box's previous deployment. Existing rows start at "" (the field is then omitted
and the console falls back to its host match) until the next sign-in fills it.

Revision ID: 0199
Revises: 0198
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0199"
down_revision: Union[str, None] = "0198"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_link",
                  sa.Column("deployment_id", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("sync_link", "deployment_id")
