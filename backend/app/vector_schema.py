"""Keep the pgvector column widths tracking EMBED_DIM — on EVERY startup, not once.

Migration 0019 sized the columns from ``settings.embed_dim`` at the moment it ran, so
changing EMBED_DIM afterwards silently did nothing. Migration 0037 tried to fix that by
CONVERGING rather than setting — but a migration converges exactly once, because alembic
stamps a revision applied and never re-runs it.

That is not a hypothetical. 0037 shipped and ran while EMBED_DIM was still unset, found
384 == 384, no-op'd, and marked itself done. The later change to EMBED_DIM=1024 was
therefore skipped entirely: the live schema stayed at vector(384) while the embedder
began emitting 1024-wide vectors, so every embedding write would fail with
``expected 384 dimensions, not 1024`` — and because ingest is failure-tolerant (AL-136),
it would fail QUIETLY, storing text with no vector while search looked fine.

The fix is placement, not logic. Convergence tracks *configuration*, and configuration
changes independently of schema revisions — so it belongs in startup, after
``upgrade head``, running on every boot. A no-op when the widths already agree (the
overwhelmingly common case, costing two catalog queries), a rebuild when they don't.

Rebuilding DROPS existing vectors (derived data — the source text is untouched).
Re-populate with ``POST /api/memory/backfill``, which re-embeds shards AND code nodes
with the current provider. It says so loudly when the table was not empty.

Migration 0037 is deliberately left frozen with its own copy of this logic: an applied
migration should never change behaviour retroactively. This module is the live path.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa

from app.config import settings

# (table, hnsw index name)
VECTOR_COLS = [
    ("memory_shards", "ix_memory_shards_embedding"),
    ("code_nodes", "ix_code_nodes_embedding"),
]

_DECLARED = sa.text(
    """
    SELECT format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = :t
      AND a.attname = 'embedding'
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND n.nspname = current_schema()
    """
)


def current_dim(bind, table: str) -> int | None:
    """Live width of ``table.embedding``, or None when the table/column isn't there."""
    declared = bind.execute(_DECLARED, {"t": table}).scalar()
    if not declared:
        return None
    m = re.match(r"vector\((\d+)\)", declared)
    return int(m.group(1)) if m else None


logger = logging.getLogger("graphban.vector_schema")


def converge(bind, target: int) -> list[tuple[str, int, int]]:
    """Rebuild every vector column whose width differs from ``target``.

    Returns ``(table, from_dim, to_dim)`` per rebuild — empty when everything already
    matches, which must stay the cheap path since this runs on every boot.
    """
    target = int(target)  # interpolated into DDL below; never let it be anything else
    changed: list[tuple[str, int, int]] = []
    for table, ix in VECTOR_COLS:
        current = current_dim(bind, table)
        if current is None or current == target:
            continue

        populated = (
            bind.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL")  # noqa: S608
            ).scalar()
            or 0
        )
        note = (
            f"; DROPPING {populated} existing vector(s) — run POST /api/memory/backfill"
            if populated
            else ""
        )
        logger.info("vector-schema: %s.embedding %s -> %s%s", table, current, target, note)

        bind.execute(sa.text(f"DROP INDEX IF EXISTS {ix}"))
        bind.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN embedding"))
        bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN embedding vector({target})"))
        bind.execute(
            sa.text(f"CREATE INDEX {ix} ON {table} USING hnsw (embedding vector_cosine_ops)")
        )
        changed.append((table, current, target))
    return changed


def converge_all() -> list[tuple[str, int, int]]:
    """Startup entry point: converge the live schema on the configured EMBED_DIM."""
    from app.db import engine

    if engine.url.drivername.startswith("sqlite"):
        return []  # SQLite stores embeddings as Text — no width to track
    with engine.begin() as conn:
        return converge(conn, settings.embed_dim)
