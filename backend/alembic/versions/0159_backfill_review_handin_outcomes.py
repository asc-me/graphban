"""Backfill delegation outcomes a builder's post-review release pre-empted (GRPH-1226).

Revision ID: 0159
Revises: 0158

`items.release_item` used to write `released` onto the builder's Delegation even when the item
sat in `review`, and `on_outcome` keeps the first outcome — so the reviewer's `signed_off` /
`bounced` found no open row. The verdict is on record in `harness_review_checks`
(`checked_at`, written when the verdict lands); a `released` row whose own check was written
AFTER its `finished_at` is such an attempt. A release with no later verdict (genuine hand-back
of unfinished work) is untouched.

A rewritten attempt ends where it would have ended had the fix been live: at the verdict.
`finished_at` moves to the check's `checked_at`, and the telemetry fields `harness.derive`
computes from the ending are re-derived with the same functions — `outcome`,
`bounce_category` (from the item's `bounce_reason`, not the check's, which was copied from a
`released` row and is NULL), `capabilities` (which drops the E1 tag a `released` ending adds),
`claim_to_finish_s` and `derived_at`. The check row's copied `capabilities` and
`bounce_category` are corrected the same way. Fields that do not depend on the ending (vendor,
model, lane, task class, size band, attempt number) are left as they are.

Core SQL only, no ORM session: the ORM models are the head schema, and a database upgrading
through 0159 to a later head would not have the later columns yet. `harness.capabilities`
and `harness.bounce_category` are pure functions of the values passed in.

Idempotent: it only rewrites `released` rows, and a rewritten row is no longer `released`.
`backfill(bind, dry_run=True)` returns the count without writing.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0159"
down_revision: Union[str, None] = "0158"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_delegations = sa.table(
    "delegations", sa.column("id"), sa.column("item_id"), sa.column("outcome"),
    sa.column("claimed_at", sa.DateTime(timezone=True)),
    sa.column("finished_at", sa.DateTime(timezone=True)))
_checks = sa.table(
    "harness_review_checks", sa.column("id"), sa.column("delegation_id"), sa.column("verdict"),
    sa.column("bounce_category"), sa.column("capabilities", sa.JSON),
    sa.column("checked_at", sa.DateTime(timezone=True)))
_telemetry = sa.table(
    "attempt_telemetry", sa.column("id"), sa.column("delegation_id"), sa.column("outcome"),
    sa.column("bounce_category"), sa.column("capabilities", sa.JSON),
    sa.column("diff_shape", sa.JSON), sa.column("tool_errors"), sa.column("turns_used"),
    sa.column("claim_to_finish_s"), sa.column("derived_at", sa.DateTime(timezone=True)))
_items = sa.table(
    "items", sa.column("id"), sa.column("touchpoints", sa.JSON), sa.column("tags", sa.JSON),
    sa.column("evidence", sa.JSON), sa.column("description"), sa.column("prd_id"),
    sa.column("prd_section"), sa.column("branch"), sa.column("bounce_reason"))


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _item(bind, item_id):
    if not item_id:
        return None
    row = bind.execute(sa.select(_items).where(_items.c.id == item_id)).first()
    return SimpleNamespace(**row._mapping) if row is not None else None


def _rederive(item, tel, *, outcome: str, finished, claimed) -> dict:
    """The ending-dependent half of `harness.derive`, from the same functions."""
    from app.services import harness

    category = harness.bounce_category(getattr(item, "bounce_reason", None)) \
        if outcome == "bounced" else None
    caps = harness.capabilities(item, tel.diff_shape if tel is not None else None, {
        "outcome": outcome,
        "bounce_category": category,
        "tool_errors": tel.tool_errors if tel is not None else None,
        "turns_used": tel.turns_used if tel is not None else None,
        "evidence": list(item.evidence or []) if item is not None else [],
        "has_branch": bool(getattr(item, "branch", None)) if item is not None else None,
    })
    return {
        "outcome": outcome,
        "bounce_category": category,
        "capabilities": caps,
        "claim_to_finish_s": (int((finished - claimed).total_seconds())
                              if claimed and finished and finished >= claimed else None),
        "derived_at": datetime.now(timezone.utc),
    }


def backfill(bind, *, dry_run: bool = False) -> int:
    """Rewrite pre-empted `released` rows; return how many delegations changed."""
    released = bind.execute(
        sa.select(_delegations.c.id, _delegations.c.item_id, _delegations.c.claimed_at,
                  _delegations.c.finished_at)
        .where(_delegations.c.outcome == "released")).all()
    changed = 0
    for did, item_id, claimed, finished in released:
        checks = bind.execute(
            sa.select(_checks.c.id, _checks.c.verdict, _checks.c.checked_at)
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
        ended = _aware(verdict.checked_at)
        bind.execute(_delegations.update().where(_delegations.c.id == did)
                     .values(outcome=verdict.verdict, finished_at=ended))
        item = _item(bind, item_id)
        tel = bind.execute(sa.select(_telemetry).where(_telemetry.c.delegation_id == did)).first()
        values = _rederive(item, tel, outcome=verdict.verdict, finished=ended,
                           claimed=_aware(claimed))
        if tel is not None:
            bind.execute(_telemetry.update().where(_telemetry.c.id == tel.id).values(**values))
        bind.execute(_checks.update().where(_checks.c.id == verdict.id).values(
            capabilities=values["capabilities"], bounce_category=values["bounce_category"]))
    return changed


def upgrade() -> None:
    backfill(op.get_bind())


def downgrade() -> None:
    # A rewritten outcome is the truth; there is nothing to restore.
    pass
