"""Re-embedding the corpus, batched, one table at a time, resumable (PRD-25 S4b, GRPH-536).

Sized on real data measured on this deployment (shards avg 444 chars, nodes 199):

    memory_shards  1,103 rows @ ~24 ms/row (batched)  ->  27s
    code_nodes       782 rows @ ~20 ms/row            ->  16s
                                                          ~43s

**Batch, do not loop.** Batch 64 gives 24.8 ms/row and batch 128 gives 24.0 — nothing left to
tune past 64, while a round trip per row is an order of magnitude worse. The batch size is a
constant here rather than a setting, because the measurement says the curve is already flat.

**One table at a time**, so a search over memory is fully old or fully new rather than half of
each. The real cost of a re-index is not its duration, it is that search spans two embedding
spaces while it runs — and finishing one table before starting the next at least keeps each
table internally consistent.

**Progress lives per table**, in `ReindexProgress`. See that model for why one counter cannot
answer the question.

**It does not take the deployment down.** `bge-m3` (0.7 GB) and the chat model (22.3 GB) stay
resident together, sharing compute rather than evicting each other; chat latency under flat-out
embed load was measured at 1.37x. A slow afternoon, not a maintenance window.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CodeNode, MemoryShard, ReindexProgress
from app.services.embedder import resolve_embedder

logger = logging.getLogger("graphban.reindex")

#: Rows per embedding request. Flat past 64 on the measurement above, so larger buys nothing
#: and only widens the blast radius of a failed batch.
BATCH = 64

#: **Ordered, and the order is the contract.** `memory_shards` completes before `code_nodes`
#: starts. Interleaving would leave both tables half-migrated for the whole run, which is the
#: one thing "one table at a time" exists to prevent.
TABLES: tuple[tuple[str, type, str], ...] = (
    ("memory_shards", MemoryShard, "text"),
    ("code_nodes", CodeNode, "summary"),
)


def _text_of(row, field: str) -> str:
    return getattr(row, field, None) or ""


def plan(db: Session, scope: str = "", *, restart: bool = False) -> list[ReindexProgress]:
    """Ensure a run exists. Returns the progress rows.

    **`restart` exists because two callers want opposite things**, and the first version of
    this function conflated them:

    - a PROCESS that just booted calls `plan()` and must find the existing progress untouched,
      or a resume silently becomes a restart — the exact bug this slice is built against;
    - an OPERATOR starting a new re-index (they changed the embedder again) calls
      `plan(restart=True)` and must get counters back at zero.

    Defaulting to the safe one matters: the dangerous call is the one that discards progress,
    so it is the one that has to be asked for by name.
    """
    rows = []
    for name, model, _field in TABLES:
        existing = db.get(ReindexProgress, (scope or "", name))
        if existing is not None and not restart:
            rows.append(existing)
            continue
        total = db.execute(select(func.count()).select_from(model)).scalar_one()
        if existing is None:
            existing = ReindexProgress(scope=scope or "", table_name=name)
            db.add(existing)
        existing.total = total
        existing.done = 0
        existing.finished_at = None
        existing.started_at = datetime.now(timezone.utc)
        rows.append(existing)
    db.commit()
    return rows


def current(db: Session, scope: str = "") -> ReindexProgress | None:
    """The table being worked, in `TABLES` order. `None` when nothing is outstanding.

    Ordering by the tuple rather than by anything in the database is what makes
    one-table-at-a-time true: the second table is not even looked at until the first is
    finished.
    """
    for name, _model, _field in TABLES:
        row = db.get(ReindexProgress, (scope or "", name))
        if row is not None and row.finished_at is None:
            return row
    return None


def run_batch(db: Session, scope: str = "") -> int:
    """Re-embed one batch of the current table. Returns how many rows were written.

    Returns 0 when there is nothing outstanding, which is how the background loop knows to stop
    without needing a separate "is it running" flag.
    """
    progress = current(db, scope)
    if progress is None:
        return 0

    model, field = next((m, f) for n, m, f in TABLES if n == progress.table_name)
    rows = db.execute(
        select(model).order_by(model.id).offset(progress.done).limit(BATCH)
    ).scalars().all()

    if not rows:
        progress.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("re-index finished %s (%d rows)", progress.table_name, progress.done)
        return 0

    embedder = resolve_embedder(db, scope)
    texts = [_text_of(r, field) for r in rows]

    # `embed_many` when the provider has it, else one call per row. The fallback is honest
    # rather than hidden: a provider without batching is slower and this says so once.
    batch_embed = getattr(embedder, "embed_many", None)
    if callable(batch_embed):
        vectors = batch_embed(texts)
    else:
        logger.info("%s has no embed_many; re-indexing one row per request",
                    type(embedder).__name__)
        vectors = [embedder.embed(t) for t in texts]

    for row, vector in zip(rows, vectors):
        row.embedding = vector

    progress.done += len(rows)
    if progress.done >= progress.total:
        progress.finished_at = datetime.now(timezone.utc)
        logger.info("re-index finished %s (%d rows)", progress.table_name, progress.done)
    db.commit()
    return len(rows)


def status(db: Session, scope: str = "") -> dict:
    """What a caller — or an operator — can see about a run in flight."""
    rows = [db.get(ReindexProgress, (scope or "", name)) for name, _m, _f in TABLES]
    present = [r for r in rows if r is not None]
    return {
        "running": any(r.finished_at is None for r in present),
        "tables": [
            {"table": r.table_name, "total": r.total, "done": r.done,
             "finished": r.finished_at is not None}
            for r in present
        ],
    }
