"""A backlog or next item carrying a blocker becomes blocked (GRPH-1961).

No new column. `status` and `blocker` are two fields, and a planner moving a
blocked item to `backlog` left its blocker text behind: the item was unready
with nothing on the board saying so. From GRPH-1961 `update_item` keeps them in
agreement; this moves the rows written before that onto `blocked`, and logs
how many it moved. Idempotent: a second run finds no such row.

Downgrade does not undo it: a row already blocked looks the same as one moved here.

Revision ID: 0233
Revises: 0232
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0233"
down_revision: Union[str, None] = "0232"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def backfill(bind) -> int:
    """Set `blocked` on backlog/next rows with a non-blank blocker. Returns rows written."""
    rows = bind.execute(sa.text(
        "SELECT id, blocker FROM items WHERE status IN ('backlog', 'next') "
        "AND blocker IS NOT NULL AND blocker <> ''"
    )).all()
    ids = [row.id for row in rows if (row.blocker or "").strip()]
    for item_id in ids:
        bind.execute(sa.text("UPDATE items SET status = 'blocked' WHERE id = :id"),
                     {"id": item_id})
    return len(ids)


def upgrade() -> None:
    written = backfill(op.get_bind())
    log.info("0233: %d backlog/next item(s) with a blocker moved to blocked", written)


def downgrade() -> None:
    pass
