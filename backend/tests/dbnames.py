"""Which database an xdist worker owns.

**Its own module because it must be importable without side effects.** It began life in
`conftest.py`, which rewrites `DATABASE_URL` at import time — and a test importing the
helper from there loaded conftest a SECOND time under a different module name, ran the
rewrite again, and produced `graphban_test_gw0_gw0`. Eighteen stray databases before anyone
noticed, and nothing failed: the engine was already bound to the first name, so the damage
was invisible to the suite that caused it.
"""
from __future__ import annotations


def drop_if_ours(url: str, worker: str) -> bool:
    """Drop `url`'s database, but ONLY if this suite derived the name. Returns whether it did.

    The destructive half of GRPH-534, kept here so it can be tested against real throwaway
    databases rather than trusted. `owns()` alone was not enough: it was fully tested while
    the caller's use of it was not, and deleting that call from `conftest` broke no test at
    all — the sabotage pass caught exactly that.

    Returns a bool rather than raising on a refusal, because declining to delete somebody
    else's database is the normal, correct outcome, not an error.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    name = parsed.database
    if not owns(name, worker):
        return False
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.begin() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.dispose()
    return True


def refuse_if_in_use(admin_url: str, name: str) -> None:
    """Refuse to adopt a database another test run is already using (GRPH-534).

    Since GRPH-529 the per-test reset is `DROP DATABASE … ; CREATE DATABASE … TEMPLATE …`,
    so two runs against one server no longer interleave rows — one DROPS the database the
    other is connected to. The second run dies with thousands of
    `FATAL: database "…_gw1" does not exist`, none of which point at the cause. It happened,
    and it read exactly like a regression in the code under test.

    Checked BEFORE this process connects, so any backend on that database belongs to somebody
    else. A refusal rather than a per-run database name: isolating would work, and would trade
    one loud failure for unbounded disk growth (see the teardown below), and two concurrent
    local runs are a mistake rather than a use case.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            busy = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = :n"), {"n": name}
            ).scalar() or 0
    finally:
        engine.dispose()
    if busy:
        raise RuntimeError(
            f"another test run is using {name} ({busy} connection(s)). Two runs against one "
            f"Postgres now delete each other's databases, not just each other's rows — wait "
            f"for the first to finish, or point this one somewhere else with DATABASE_URL."
        )


def owns(database: str | None, worker: str) -> bool:
    """Did THIS suite derive that database name, and may it therefore drop it? (GRPH-534)

    The destructive half of the cleanup, so it is a separate, pure, tested decision rather
    than an `endswith` inline at the call site. `<base>_gw3` is an artifact this suite
    created and recreates on demand. `<base>` is somebody's — one machine's 76 leftover test
    databases included a PRD-22 walk — and a `pytest` invocation that deleted it would be a
    far worse bug than the accumulation being fixed.

    Lives here rather than in `conftest`, for the reason this module exists at all: importing
    `conftest` rewrites `DATABASE_URL`, so a test that imported this from there would trigger
    the rewrite a second time. That is not hypothetical — it is the `graphban_test_gw0_gw0`
    story above.
    """
    return bool(database) and bool(worker) and database.endswith(f"_{worker}")


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
