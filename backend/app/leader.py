"""Single-runner coordination for the lifespan when uvicorn runs N workers (GRPH-1241).

Two different needs, two different locks, both Postgres session-level advisory locks:

* `boot_lock()` serialises the once-per-deploy boot work (migrations, seed, credential
  migration). Workers queue behind it, so the first one does the work and the rest find
  it already done — every one of those steps is idempotent, which is what makes waiting
  correct where skipping would not be (a skipped worker would start serving before the
  schema exists).
* `claim_background_leader()` is a non-blocking try-lock held for the life of the process.
  Exactly one worker wins and runs the background loops; the lock is released by Postgres
  when that process dies, so a respawned worker can take over.

SQLite is always a single process (`serve.effective_workers` forces it), so both are no-ops.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from app.config import settings
from app.db import engine

logger = logging.getLogger("graphban.leader")

# Arbitrary, fixed, distinct from each other.
_BOOT_KEY = 0x4752_4248_0001
_LEADER_KEY = 0x4752_4248_0002

_leader_conn = None


@contextmanager
def boot_lock() -> Iterator[None]:
    if settings.is_sqlite:
        yield
        return
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _BOOT_KEY})
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _BOOT_KEY})
            conn.commit()


def claim_background_leader() -> bool:
    """True if this process should run the background loops. Holds the lock until exit."""
    global _leader_conn
    if settings.is_sqlite:
        return True
    if _leader_conn is not None:
        return True
    conn = engine.connect()
    won = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LEADER_KEY}).scalar())
    conn.commit()
    if won:
        _leader_conn = conn
        return True
    conn.close()
    return False


def release_background_leader() -> None:
    global _leader_conn
    if _leader_conn is not None:
        try:
            # Unlock explicitly: `close()` only returns the connection to the pool, where the
            # session — and the lock with it — would live on and block a successor.
            _leader_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LEADER_KEY})
            _leader_conn.commit()
        finally:
            _leader_conn.close()
            _leader_conn = None
