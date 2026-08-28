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


def unlink_if_ours(url: str, worker: str) -> bool:
    """Delete this worker's SQLite file at session end, but ONLY if this suite derived it.

    **The SQLite half of `drop_if_ours`.** GRPH-534 stopped Postgres worker databases
    accumulating — 76 of them, 771 MB, on one machine — and `_drop_worker_database` returned
    early on SQLite, so `backend/.pytest_gw*.db` kept doing exactly the same thing: eighteen
    files at ~888 KB apiece were sitting in a working tree earlier tonight. GRPH-554's
    acceptance asked for this and it was not done; the concurrency lock shipped instead.

    Ownership is decided by the SAME pure function Postgres uses, applied to the file's stem:
    `.pytest_gw3` ends with `_gw3` and is an artifact this suite creates on demand, while
    `.pytest` does not and is somebody's. A `pytest` invocation that deleted the base file
    would be a far worse bug than the accumulation being fixed — that is `owns()`'s own
    argument and it transfers unchanged.

    **The `.lock` file is deliberately left behind**, which is the one place this does not
    mirror Postgres. It is empty, it is gitignored, and removing it opens a race: a second run
    that has OPENED the lock but not yet flocked it would end up holding an unlinked inode,
    after which a third run creates a fresh file and also succeeds — two runs both believing
    they hold the claim. The lock being correct matters more than the directory being tidy.

    Returns whether it deleted anything, so a refusal is an ordinary outcome rather than an
    error — same shape as `drop_if_ours`.
    """
    import pathlib as _pathlib

    if not url.startswith("sqlite"):
        return False
    path = url.split("///", 1)[-1]
    if not path or path.endswith(":memory:"):
        return False

    target = _pathlib.Path(path)
    if not owns(target.stem, worker):
        return False

    # The sidecars are ours too, and a stale journal beside a deleted database is its own
    # source of confusion — SQLite will try to roll it back into whatever appears next.
    for suffix in ("", "-journal", "-wal", "-shm"):
        _pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
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


class NotIsolated(RuntimeError):
    """A per-worker URL was not derived, so every worker would share one database.

    Raised by the CALLER (`conftest._database_per_worker`) rather than by `worker_url`, and
    that placement is the point. GRPH-568 asked for a refusal instead of a silent no-op; with
    the path-based derivation below a no-op can no longer happen, so a check inside
    `worker_url` would be unreachable code guarding against nothing — which is the shape this
    repository keeps filing defects about. At the call site it stays reachable: it fires if a
    future edit to the derivation stops isolating, which is exactly the regression that cost
    2,659 errors.
    """


def worker_url(url: str, worker: str) -> str:
    """The database `worker` owns, derived from the base URL.

    Pure, so it can be tested without a server — and so importing it cannot rewrite
    anyone's environment.

    **Derived from the PATH, not by replacing a substring** (GRPH-568). This used to be
    `url.replace(".pytest.db", f".pytest_{worker}.db")`, which silently did nothing for any
    sqlite name that was not the default one — every worker then resolved to the same file and
    raced to create the schema, producing thousands of `table users already exists` errors that
    point nowhere near the cause.

    That mattered because it broke the escape hatch GRPH-554's lock message recommends by name:
    *"point this one somewhere else with `DATABASE_URL`"*. Doing exactly that worked serially
    and defeated `-n auto`, which is how CI and everyone else runs the suite. Measured:
    `sqlite:///./.pytest-v2.db` gave 2,659 errors against `sqlite:///./.mine/.pytest.db`
    passing 2,746 — the second only because it happened to keep the magic substring.

    The string surgery is deliberate rather than `PurePath`: `PurePosixPath("./.pytest.db")`
    normalises away the `./`, which would rewrite a URL the caller wrote by hand.
    """
    if url.startswith("sqlite"):
        prefix, sep, path = url.partition("///")
        if not sep or path.endswith(":memory:"):
            # An in-memory database is per-process already, so workers cannot collide on it.
            return url
        head, slash, filename = path.rpartition("/")
        stem, dot, ext = filename.rpartition(".")
        derived = (f"{stem}_{worker}{dot}{ext}" if dot else f"{filename}_{worker}")
        return f"{prefix}{sep}{head}{slash}{derived}"

    base, _, name = url.rpartition("/")
    return f"{base}/{name}_{worker}"


#: Held for the lifetime of the session, keyed by the database path. A `flock` lives on the
#: open file DESCRIPTION, so the handle has to outlive the function that took it — drop it and
#: the lock silently releases, which is the failure mode that looks exactly like the guard
#: working.
#:
#: Keyed by path rather than a single slot, because "already claimed something" and "already
#: claimed THIS" are different questions. A single slot answers the first and silently returns
#: success for a database it never locked.
_sqlite_locks: dict[str, object] = {}


def claim_sqlite(url: str) -> bool:
    """Refuse to run when another test process already holds this SQLite database.

    **The SQLite half of GRPH-534, and the reason GRPH-554 kept recurring.** That item gave
    Postgres `refuse_if_in_use`; SQLite got nothing, so two runs in one working tree happily
    shared one file. What made it destructive rather than merely racy is that the suite
    deletes and rebuilds that file — so one run unlinks the database the other has open.

    Every signature recorded on GRPH-554 follows from that single act, which is why they never
    resolved into one cause while they were being read as three:

    * an unlinked inode under an open connection -> `attempt to write a readonly database`,
      `disk I/O error`, `malformed database schema (X) - table X already exists`
    * a new connection creating a fresh empty file -> `no such table: projects`
    * a re-seed landing in a half-populated file -> `UNIQUE constraint failed: projects.tag`

    Reproduced deliberately: two suites started four seconds apart in one worktree, each of
    which passes alone (44 passed), corrupted BOTH runs — 2 failed / 16 errors in the second
    and 7 failed / 17 errors in the first, the one that was minding its own business.

    `flock` rather than a PID file: the kernel releases it when the process dies, however it
    dies. A PID file has to be cleaned up by the thing that crashed, and a stale one refuses
    every future run — turning a real guard into a thing people delete by habit.

    Returns True when the claim succeeded, so a caller can tell "not applicable" (Postgres)
    from "claimed". Refusal RAISES, because continuing is what does the damage.
    """
    if not url.startswith("sqlite"):
        return False

    import fcntl
    import pathlib

    path = url.split("///", 1)[-1] or ".pytest.db"
    if path in _sqlite_locks:         # already claimed by this process; re-entrant like the
        return True                   # URL rewrite above, and for the same reason
    lock_path = pathlib.Path(path + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            f"another test run already holds {path}. Two runs in one working tree share this "
            f"file, and the suite DELETES and rebuilds it — so one run unlinks the database "
            f"the other has open. That does not fail cleanly: it surfaces as 'no such table', "
            f"'attempt to write a readonly database', 'malformed database schema' or a UNIQUE "
            f"violation in the seed, none of which point at the cause (GRPH-554). Wait for the "
            f"first run to finish, or point this one somewhere else with DATABASE_URL."
        ) from None

    _sqlite_locks[path] = handle
    return True


def release_sqlite(url: str | None = None) -> None:
    """Drop a claim, or all of them. Only needed by tests — a real session holds until the
    process ends, which is the point of using `flock`."""
    paths = ([url.split("///", 1)[-1]] if url else list(_sqlite_locks))
    for path in paths:
        handle = _sqlite_locks.pop(path, None)
        if handle is not None:
            handle.close()            # closing releases the flock
