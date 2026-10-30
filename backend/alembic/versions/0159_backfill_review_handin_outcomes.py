"""Backfill delegation outcomes a builder's post-review release pre-empted (GRPH-1226).

Revision ID: 0159
Revises: 0158

`items.release_item` used to write `released` onto the builder's Delegation even when the item
sat in `review`, and `on_outcome` keeps the first outcome — so the reviewer's `signed_off` /
`bounced` found no open row. The verdict is on record in `harness_review_checks`
(`checked_at`, written when the verdict lands); a `released` row whose own check was written AFTER its `finished_at` is such an
attempt. A release with no later verdict (genuine hand-back of unfinished work) is untouched.

Idempotent: it only rewrites `released` rows, and a rewritten row is no longer `released`.
`backfill(bind, dry_run=True)` returns the count without writing.
"""

from datetime import timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0159"
down_revision: Union[str, None] = "0158"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_delegations = sa.table(
    "delegations", sa.column("id"), sa.column("outcome"), sa.column("finished_at", sa.DateTime(timezone=True)))
_checks = sa.table(
    "harness_review_checks", sa.column("delegation_id"), sa.column("verdict"),
    sa.column("bounce_category"), sa.column("checked_at", sa.DateTime(timezone=True)))
_telemetry = sa.table(
    "attempt_telemetry", sa.column("delegation_id"), sa.column("outcome"),
    sa.column("bounce_category"))


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def backfill(bind, *, dry_run: bool = False) -> int:
    """Rewrite pre-empted `released` rows; return how many delegations changed."""
    released = bind.execute(
        sa.select(_delegations.c.id, _delegations.c.finished_at)
        .where(_delegations.c.outcome == "released")).all()
    changed = 0
    for did, finished in released:
        checks = bind.execute(
            sa.select(_checks.c.verdict, _checks.c.bounce_category, _checks.c.checked_at)
            .where(_checks.c.delegation_id == did,
                   _checks.c.verdict.in_(("signed_off", "bounced")))).all()
        later = [c for c in checks
                 if c.checked_at is not None and finished is not None
                 and _aware(c.checked_at) >= _aware(finished)]
        if not later:
            continue
        verdict = max(later, key=lambda c: _aware(c.checked_at))
        changed += 1
        if dry_run:
            continue
        bind.execute(_delegations.update().where(_delegations.c.id == did)
                     .values(outcome=verdict.verdict))
        values = {"outcome": verdict.verdict}
        if verdict.verdict == "bounced":
            values["bounce_category"] = verdict.bounce_category
        bind.execute(_telemetry.update().where(_telemetry.c.delegation_id == did)
                     .values(**values))
    return changed


def upgrade() -> None:
    backfill(op.get_bind())


def downgrade() -> None:
    # A rewritten outcome is the truth; there is nothing to restore.
    pass
