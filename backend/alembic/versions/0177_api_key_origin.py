"""`api_keys.origin`: which surface minted a credential (GRPH-1310).

NULL is unknown, not `web`. Backfill is by the shapes the minting surfaces leave behind.

Revision ID: 0177
Revises: 0176
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0177"
down_revision: Union[str, None] = "0176"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("origin", sa.String(), nullable=True))

    conn = op.get_bind()
    keys = sa.table(
        "api_keys",
        sa.column("id", sa.String), sa.column("name", sa.String),
        sa.column("scopes", sa.JSON), sa.column("project_id", sa.String),
        sa.column("fleet_wave", sa.String), sa.column("origin", sa.String),
    )
    # Scopes is JSON; compare in Python so SQLite and Postgres agree.
    for row in conn.execute(sa.select(keys.c.id, keys.c.name, keys.c.scopes,
                                      keys.c.project_id, keys.c.fleet_wave)).fetchall():
        scopes = row.scopes or []
        if isinstance(scopes, str):
            import json
            scopes = json.loads(scopes)
        origin = None
        if row.fleet_wave is not None:
            origin = "fleet"
        elif "gate" in scopes and row.project_id and row.name == f"{row.project_id} swamp gate":
            origin = "gban_swamp_setup"
        elif (row.project_id and "gate" not in scopes and "sync" not in scopes
              and row.name == f"{row.project_id} delegation"):
            origin = "gban_setup"
        if origin:
            conn.execute(keys.update().where(keys.c.id == row.id).values(origin=origin))


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("origin")
