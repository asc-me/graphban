"""Retire the memory shards a pr_cooldown gate refusal wrote (GRPH-1234).

Revision ID: 0160
Revises: 0159

`items.record_refusal` no longer writes a shard for `pr_cooldown`: it is a wait that resolves
itself, and the refusal is already an evidence note on the item. The shards written before that
change sit in the Memory "Stale or wrong" queue and ride later briefs as lessons ("the cooldown
had 18s left"). This retires them with the same fields `memory.retire_shard` sets — `status`
`retired` and `retired_at` — so each is out of retrieval at once and restorable for
RETIRE_RECOVERY_DAYS. Nothing is deleted.

Only `published` shards with source exactly `gate refusal: pr_cooldown` are touched; every other
gate-refusal source is left alone. Idempotent: a retired shard no longer matches.
`retire(bind, dry_run=True)` returns the count without writing.

Core SQL only, no ORM session, so a database upgrading through 0160 does not depend on later
columns.
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0160"
down_revision: Union[str, None] = "0159"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCE = "gate refusal: pr_cooldown"

_shards = sa.table(
    "memory_shards", sa.column("id"), sa.column("source"), sa.column("status"),
    sa.column("retired_at", sa.DateTime(timezone=True)))


def retire(bind, *, dry_run: bool = False) -> int:
    """Retire published pr_cooldown refusal shards; return how many changed."""
    match = sa.and_(_shards.c.source == SOURCE, _shards.c.status == "published")
    if dry_run:
        return bind.execute(sa.select(sa.func.count()).select_from(_shards).where(match)).scalar() or 0
    result = bind.execute(_shards.update().where(match).values(
        status="retired", retired_at=datetime.now(timezone.utc)))
    return result.rowcount or 0


def upgrade() -> None:
    retire(op.get_bind())


def downgrade() -> None:
    # Retirement is recoverable through the memory UI; a bulk restore would also revive
    # shards a person retired for another reason.
    pass
