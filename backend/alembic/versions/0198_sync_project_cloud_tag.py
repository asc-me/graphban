"""sync_project.cloud_project_tag: the cloud project's tag, shown as ☁ TAG (GRPH-1562).

The console answers `GET /api/sync/projects` with `{id, tag, name}` per project; the box kept
only the id and rendered it as the tag. The tag is written when a project is mapped and
refreshed on every later projects read. Existing rows start at "" and render `☁ ?` until
then: the migration never calls the console.

Revision ID: 0198
Revises: 0197
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0198"
down_revision: Union[str, None] = "0197"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_project",
                  sa.Column("cloud_project_tag", sa.String(), nullable=True, server_default=""))


def downgrade() -> None:
    op.drop_column("sync_project", "cloud_project_tag")
