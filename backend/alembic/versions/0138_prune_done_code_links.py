"""Delete stored `code` links that touch a done item (GRPH-1068).

`sync_code_links` used to link a new item to every item sharing a touchpoint, done or not,
and nothing ever removed a `code` link — so GRPH-1063 was born with 210 of them, 178 to
finished work. The runtime now links open items only and prunes on completion; this sweeps
what is already stored. Only `code` links: `dependency` and `semantic` are authored and stay.

Portable SQL (no dialect-specific DELETE … USING), so `sweep` runs the same on SQLite and
Postgres. The count it removed is logged.

Revision ID: 0138
Revises: 0137
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0138"
down_revision: Union[str, None] = "0137"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

_SWEEP = """
DELETE FROM links
WHERE type = 'code'
  AND (a IN (SELECT id FROM items WHERE status = 'done')
       OR b IN (SELECT id FROM items WHERE status = 'done'))
"""


def sweep(conn) -> int:
    """Run the delete on `conn`; return the number of links it removed."""
    return conn.execute(sa.text(_SWEEP)).rowcount or 0


def upgrade() -> None:
    removed = sweep(op.get_bind())
    log.info("0138: removed %d code link(s) touching done items", removed)


def downgrade() -> None:
    # The deleted rows are derived data: `related_items` recomputes overlap live, and an item
    # that reopens re-links to open work. Nothing to restore.
    pass
