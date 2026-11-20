"""Backfill open unscoped fixes onto the project's maintenance line (GRPH-1798).

No new column. An open fix (`status` other than `done`) with neither `prd_id` nor
`line_id` is stored on its project's line when the project has one. A project
with no line is left alone. An open item with an empty `kind` and a `bug` tag is
treated as a fix: it gets `kind=fix`, and the line when one exists. An item tagged
`feature`, or with `kind=feature`, is not touched. A `done` row is not touched.

Core SQL plus a small Python pass over the rows. Tag membership is a JSON list,
and SQLite and Postgres do not agree on how a JSON array is queried; the test
runs this function on SQLite while production runs it on Postgres. Idempotent:
a row that already has a scope is skipped, and a second run writes nothing.

Downgrade does not undo the assignment. The rows that were already on the line
cannot be told apart from the rows this put there.

Revision ID: 0223
Revises: 0222
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0223"
down_revision: Union[str, None] = "0222"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _names(raw) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return set()
    if not isinstance(raw, list):
        return set()
    return {t.strip().lower() for t in raw if isinstance(t, str) and t.strip()}


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def backfill(bind) -> int:
    """Assign the line and, for an empty-kind bug tag, `kind=fix`. Returns rows written."""
    lines = {
        row.project_id: row.id
        for row in bind.execute(sa.text("SELECT id, project_id FROM lines"))
    }
    rows = bind.execute(sa.text(
        "SELECT id, project_id, kind, tags, status, prd_id, line_id FROM items"
    )).all()
    written = 0
    for row in rows:
        if (row.status or "") == "done":
            continue
        if not _blank(row.prd_id) or not _blank(row.line_id):
            continue
        tags = _names(row.tags)
        kind = (row.kind or "").strip()
        if kind == "feature" or "feature" in tags:
            continue
        bug_tag = kind == "" and "bug" in tags
        if kind != "fix" and not bug_tag:
            continue
        line_id = lines.get(row.project_id)
        new_kind = "fix" if bug_tag else kind
        if line_id is None and new_kind == kind:
            continue
        bind.execute(
            sa.text("UPDATE items SET line_id = :line_id, kind = :kind WHERE id = :id"),
            {"line_id": line_id, "kind": new_kind, "id": row.id},
        )
        written += 1
    return written


def upgrade() -> None:
    backfill(op.get_bind())


def downgrade() -> None:
    # The assignment is not reversible: a row that was already on the line looks
    # the same as a row this migration put there.
    pass
