"""Which database an xdist worker owns.

**Its own module because it must be importable without side effects.** It began life in
`conftest.py`, which rewrites `DATABASE_URL` at import time — and a test importing the
helper from there loaded conftest a SECOND time under a different module name, ran the
rewrite again, and produced `graphban_test_gw0_gw0`. Eighteen stray databases before anyone
noticed, and nothing failed: the engine was already bound to the first name, so the damage
was invisible to the suite that caused it.
"""
from __future__ import annotations


def worker_url(url: str, worker: str) -> str:
    """The database `worker` owns, derived from the base URL.

    Pure, so it can be tested without a server — and so importing it cannot rewrite
    anyone's environment.
    """
    if url.startswith("sqlite"):
        # A file per worker. SQLite has no server to create anything on.
        return url.replace(".pytest.db", f".pytest_{worker}.db")
    base, _, name = url.rpartition("/")
    return f"{base}/{name}_{worker}"
