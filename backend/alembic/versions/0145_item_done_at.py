"""`items.done_at`: when an item last reached `done` (PRD-60 D2 / GRPH-1134).

The board's done-window and auto-hide ("hide done N days after completion") need the time of
completion. `updated_at` is not that time, because retitling a finished item bumps it.

Backfill, for rows already `done`: the newest audit event that reported this item with
`status: done` (the MCP dispatcher writes `meta.status` on every evidence-carrying write, and
sign_off and a done update_item always carry evidence). If no such event exists, `updated_at`
is used, which is the best guess left. The backfill only fills NULLs, so a second run changes
nothing.

Events name an item by its RENDERED key (`TAG-N`), and the tag can have been renamed since.
A match is therefore the same project plus the same number, or the stored id itself.

Revision ID: 0145
Revises: 0144
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0145"
down_revision: Union[str, None] = "0144"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _number_of(target_id: str) -> int | None:
    """`GRPH-12` → 12. A PRD (`GRPH-P4`) or agent (`GRPH-A7`) key has a letter and is None."""
    tail = target_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def backfill(conn) -> None:
    items = sa.table("items", sa.column("id"), sa.column("project_id"), sa.column("number"),
                     sa.column("status"), sa.column("updated_at"), sa.column("done_at"))
    events = sa.table("events", sa.column("ts"), sa.column("project_id"),
                      sa.column("target_id"), sa.column("meta", sa.JSON))

    todo = conn.execute(
        sa.select(items.c.id, items.c.project_id, items.c.number, items.c.updated_at)
        .where(items.c.status == "done", items.c.done_at.is_(None))
    ).all()
    if not todo:
        return
    by_id = {r.id: r for r in todo}
    by_number = {(r.project_id, r.number): r.id for r in todo}

    # Filtered in Python: `meta` is JSON, and a dialect-neutral JSON path is not worth it for
    # a one-off pass over rows that already carry a target.
    latest: dict[str, object] = {}
    rows = conn.execute(
        sa.select(events.c.ts, events.c.project_id, events.c.target_id, events.c.meta)
        .where(events.c.target_id != "", events.c.meta.isnot(None))
    )
    for ev in rows:
        meta = ev.meta
        if not isinstance(meta, dict) or meta.get("status") != "done":
            continue
        item_id = ev.target_id if ev.target_id in by_id else None
        if item_id is None:
            number = _number_of(ev.target_id)
            item_id = by_number.get((ev.project_id, number)) if number is not None else None
        if item_id is None:
            continue
        if item_id not in latest or ev.ts > latest[item_id]:
            latest[item_id] = ev.ts

    for r in todo:
        conn.execute(
            items.update().where(items.c.id == r.id, items.c.done_at.is_(None))
            .values(done_at=latest.get(r.id, r.updated_at))
        )


def upgrade() -> None:
    op.add_column("items", sa.Column("done_at", sa.DateTime(timezone=True), nullable=True))
    backfill(op.get_bind())


def downgrade() -> None:
    op.drop_column("items", "done_at")
