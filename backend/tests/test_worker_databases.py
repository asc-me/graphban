"""One test run at a time, and clean up after it (GRPH-534).

Two problems, one file, because they are the same eight lines of `conftest`.

**Concurrent runs now destroy rather than interleave.** Before GRPH-529, two `pytest -n auto`
runs against one Postgres shared each worker's database and truncated each other's rows —
confusing, recoverable. The per-test reset is now `DROP DATABASE … ; CREATE DATABASE …
TEMPLATE …`, so one run drops the database the other is connected to and the second dies with
thousands of `FATAL: database "…_gw1" does not exist`, none of which point at the cause. It
happened while building GRPH-360 and read exactly like a regression in the code under test.

**Worker databases were never dropped.** One machine had 76 of them, 771 MB — four base names
across eighteen workers, from every parallel run ever done against that container.

The dangerous half is the cleanup, so `owns()` is a separate pure decision with its own tests:
`<base>_gw3` is an artifact this suite created; `<base>` is somebody's, and one of those 76
was a PRD-22 walk database.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.dbnames import copy_sqlite_file, drop_if_ours, owns, refuse_if_in_use, template_name, worker_url


# ── what this suite may delete ────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["graphban_test_gw0", "graphban_test_gw11", "gb_gw3"])
def test_a_derived_worker_database_is_ours(name):
    worker = name.rsplit("_", 1)[1]
    assert owns(name, worker) is True


@pytest.mark.parametrize("name", ["graphban_test", "graphban_p22", "graphban_rev", "postgres"])
def test_a_base_database_is_never_ours(name):
    """THE guard. `graphban_p22` was a PRD-22 walk on the machine where the 76 were found;
    a `pytest` invocation that deleted it would be far worse than the leak being fixed."""
    assert owns(name, "gw0") is False


def test_another_workers_database_is_not_ours():
    """gw0 must not tidy up gw1's database — under `-n auto` every worker runs this teardown,
    and one that reached beyond its own name would delete a sibling mid-run."""
    assert owns("graphban_test_gw1", "gw0") is False


def test_a_serial_run_owns_nothing():
    """No `PYTEST_XDIST_WORKER` means no derived database, so there is nothing to drop — and
    the base is what a serial run is pointed at, which is exactly what must survive."""
    assert owns("graphban_test", "") is False
    assert owns(None, "gw0") is False


def test_owns_agrees_with_the_name_worker_url_builds():
    """Pins the two halves together. A namer and a dropper that disagreed would either leak
    every database or delete one they did not create, and both are silent."""
    for worker in ("gw0", "gw7", "gw15"):
        built = worker_url("postgresql+psycopg://u:p@h/graphban_test", worker)
        assert owns(built.rpartition("/")[2], worker) is True


# ── the refusal ───────────────────────────────────────────────────────────────

def _pg_only():
    from app.db import engine

    if engine.url.drivername.startswith("sqlite"):
        pytest.skip("Postgres-only: SQLite has no server and gets a file per worker")
    return engine


def test_a_database_someone_else_is_using_is_refused():
    """The whole point: one clear line at startup instead of a 2331-error cascade.

    Holds a connection open EXPLICITLY rather than leaning on this session having one. It
    does not — `_copy_database` disposes the pool before every test, so the engine is idle
    at this moment and the check would correctly see nothing. An earlier version assumed the
    ambient connection, and failed here honestly rather than passing by accident.
    """
    engine = _pg_only()
    # `render_as_string(hide_password=False)`, not `str()`: SQLAlchemy masks the password as
    # `***` in `str(URL)`, which then connects as `postgres` with the literal password `***`
    # and fails authentication. The real call site builds this by string surgery on the raw
    # DATABASE_URL and never meets the problem.
    admin = engine.url.set(database="postgres").render_as_string(hide_password=False)
    with engine.connect() as held:          # stands in for the other run
        held.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError) as e:
            refuse_if_in_use(admin, engine.url.database)
    assert engine.url.database in str(e.value)
    assert "DATABASE_URL" in str(e.value), "the refusal must say what to do about it"


def test_an_unused_database_is_not_refused():
    """The complement, so the check cannot degenerate into refusing every run — which would
    pass the test above perfectly and stop the suite from running at all."""
    engine = _pg_only()
    # `render_as_string(hide_password=False)`, not `str()`: SQLAlchemy masks the password as
    # `***` in `str(URL)`, which connects as user `postgres` with the literal password `***`
    # and fails authentication. The real call site builds this by string surgery on the raw
    # DATABASE_URL and never meets the problem.
    admin = engine.url.set(database="postgres").render_as_string(hide_password=False)
    refuse_if_in_use(admin, "a_database_that_does_not_exist_grph534")


# ── the destructive path, against real throwaway databases ────────────────────

def _scratch(engine, name):
    """Create a real database and yield its URL. Torn down whatever the test does."""
    from sqlalchemy import create_engine, text

    admin = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.begin() as c:
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    return admin, engine.url.set(database=name).render_as_string(hide_password=False)


def _exists(admin, name):
    from sqlalchemy import text

    with admin.begin() as c:
        return bool(c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                              {"n": name}).first())


def test_a_base_database_survives_the_teardown():
    """THE test this file was missing, and the sabotage pass is what proved it missing:
    `owns()` was thoroughly tested while nothing exercised the CALL to it, so deleting that
    call broke no test at all. This drives the real drop against a real database whose name
    has no worker suffix, and the assertion is that it is still there afterwards."""
    engine = _pg_only()
    admin, url = _scratch(engine, "grph534_base")
    try:
        assert drop_if_ours(url, "gw0") is False
        assert _exists(admin, "grph534_base"), "a base database was deleted"
    finally:
        with admin.begin() as c:
            c.exec_driver_sql('DROP DATABASE IF EXISTS "grph534_base"')
        admin.dispose()


def test_a_worker_database_is_actually_dropped():
    """The complement. A teardown that refused everything would pass the test above and
    leak exactly as before — which is the bug being fixed."""
    engine = _pg_only()
    admin, url = _scratch(engine, "grph534_scratch_gw0")
    try:
        assert drop_if_ours(url, "gw0") is True
        assert not _exists(admin, "grph534_scratch_gw0"), "the worker database was not dropped"
    finally:
        with admin.begin() as c:
            c.exec_driver_sql('DROP DATABASE IF EXISTS "grph534_scratch_gw0"')
        admin.dispose()


def test_a_worker_does_not_drop_a_siblings_database():
    """Every worker runs this teardown under `-n auto`. One that reached past its own name
    would delete a sibling mid-run, and the sibling's failures would land nowhere near it."""
    engine = _pg_only()
    admin, url = _scratch(engine, "grph534_scratch_gw1")
    try:
        assert drop_if_ours(url, "gw0") is False
        assert _exists(admin, "grph534_scratch_gw1")
    finally:
        with admin.begin() as c:
            c.exec_driver_sql('DROP DATABASE IF EXISTS "grph534_scratch_gw1"')
        admin.dispose()


def test_the_teardown_is_actually_wired_into_the_session():
    """A SOURCE READ, deliberately, and the reason is worth stating.

    Everything else here drives `drop_if_ours` directly. Nothing can observe the session-end
    teardown from inside the session it tears down — by the time it runs, every test has
    finished. So the one regression left is somebody deleting the call, and the leak that
    follows is silent: databases simply accumulate again, exactly as they did for the 76.

    Weaker than a behavioural test and stronger than nothing. The alternative — spawning a
    real xdist session against a scratch database and asserting it vanishes — is the honest
    version, and is not worth its cost for a line whose failure mode is disk usage rather
    than data loss.
    """
    import inspect

    from tests import conftest

    src = inspect.getsource(conftest._schema)
    assert "_drop_worker_database()" in src, (
        "the session fixture no longer drops the worker's database — worker databases will "
        "accumulate again, one per run, silently")


# ---- a NON-default sqlite name must isolate too (GRPH-568) --------------------------

@pytest.mark.parametrize("url, expected", [
    # The default, unchanged — this is the regression guard for everything that came before.
    ("sqlite:///./.pytest.db", "sqlite:///./.pytest_gw0.db"),
    # THE CASE THAT WAS BROKEN. The old derivation replaced the literal substring
    # `.pytest.db`, so this returned unchanged and every worker opened one file.
    ("sqlite:///./.pytest-v2.db", "sqlite:///./.pytest-v2_gw0.db"),
    ("sqlite:///./scratch/run.db", "sqlite:///./scratch/run_gw0.db"),
    ("sqlite:////abs/path/db.sqlite3", "sqlite:////abs/path/db_gw0.sqlite3"),
    # No suffix to insert before, so the id goes on the end rather than nowhere.
    ("sqlite:///./mydb", "sqlite:///./mydb_gw0"),
])
def test_any_sqlite_name_isolates(url, expected):
    """GRPH-554's lock tells a blocked run to *"point this one somewhere else with
    `DATABASE_URL`"*. Doing that worked serially and silently defeated `-n auto`, which is how
    CI and everyone runs the suite: the derivation replaced a literal substring, so any name
    that was not the default came back unchanged and every worker raced to create the schema.

    Measured on the ticket: `sqlite:///./.pytest-v2.db` gave **2,659** `table users already
    exists` errors, while `sqlite:///./.mine/.pytest.db` passed 2,746 — the second only because
    it happened to keep the magic substring in a different directory.
    """
    assert worker_url(url, "gw0") == expected


def test_the_relative_prefix_is_preserved():
    """`./` survives. Deriving through `PurePosixPath` would normalise it away and rewrite a
    URL the caller typed by hand — a small thing that makes the refusal message and the file on
    disk stop matching what was asked for."""
    assert worker_url("sqlite:///./.pytest.db", "gw1").startswith("sqlite:///./")


def test_two_workers_never_share_a_database():
    """The property the whole mechanism exists for, asserted directly rather than inferred from
    the strings above. A derivation that returned a constant would satisfy every equality test
    written one URL at a time."""
    for url in ("sqlite:///./.pytest.db", "sqlite:///./.pytest-v2.db",
                "postgresql+psycopg://u:p@h/graphban_test"):
        derived = {worker_url(url, f"gw{i}") for i in range(4)}
        assert len(derived) == 4, f"{url} does not isolate: {sorted(derived)}"
        assert url not in derived, f"{url} returned itself for some worker"


def test_an_in_memory_database_is_left_alone():
    """Exempt because it needs no isolation: an in-memory database is per-process already, so
    workers cannot collide on one. Returning it unchanged is correct, which is why the no-op
    refusal lives at the call site rather than in here."""
    assert worker_url("sqlite:///:memory:", "gw0") == "sqlite:///:memory:"


def test_a_derivation_that_stops_isolating_is_refused(monkeypatch):
    """The refusal GRPH-568 asked for, driven rather than asserted.

    It lives in `conftest._database_per_worker`, not in `worker_url`: with the path-based
    derivation a no-op can no longer happen, so a check inside that function would be
    unreachable code guarding nothing. At the call site it stays live — it fires if a future
    edit stops isolating, which is precisely the regression that cost 2,659 errors.
    """
    import sys

    from tests import dbnames

    conftest = sys.modules["conftest"]
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./scratch.db")
    # `conftest` imports `worker_url` INSIDE the function, so the module attribute is what the
    # call resolves against — patching a name on `conftest` would miss it entirely.
    monkeypatch.setattr(dbnames, "worker_url", lambda url, worker: url)   # the old bug

    with pytest.raises(dbnames.NotIsolated) as err:
        conftest._database_per_worker()

    assert "would share it" in str(err.value)
    assert "scratch.db" in str(err.value), "the refusal does not name the URL that failed"


# ---- the SQLite half of the teardown (GRPH-554 acceptance, done late) ----------------

@pytest.fixture()
def sqlite_tree(tmp_path):
    """A worker file with its sidecars, and the base file beside it — the two that must be
    told apart."""
    base = tmp_path / ".pytest.db"
    worker = tmp_path / ".pytest_gw3.db"
    for p in (base, worker):
        p.write_bytes(b"x")
    for suffix in ("-journal", "-wal", "-shm"):
        (tmp_path / f".pytest_gw3.db{suffix}").write_bytes(b"x")
    (tmp_path / ".pytest_gw3.db.lock").write_bytes(b"")
    return tmp_path


def test_a_worker_file_and_its_sidecars_are_removed(sqlite_tree):
    """The accumulation this closes: eighteen `.pytest_gw*.db` files at ~888 KB were sitting
    in a working tree tonight, because `_drop_worker_database` returned early on SQLite while
    Postgres had been cleaned up since GRPH-534.

    Sidecars go too — a stale journal beside a deleted database is its own source of
    confusion, since SQLite tries to roll it back into whatever appears next.
    """
    from tests.dbnames import unlink_if_ours

    assert unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest_gw3.db", "gw3") is True

    assert not (sqlite_tree / ".pytest_gw3.db").exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not (sqlite_tree / f".pytest_gw3.db{suffix}").exists()


def test_the_base_file_survives(sqlite_tree):
    """THE SAFETY PROPERTY, and the reason ownership is a separate pure function. `.pytest.db`
    is somebody's — a `pytest` invocation that deleted it would be a far worse bug than the
    accumulation being fixed. That is `owns()`'s own argument, applied to the file's stem."""
    from tests.dbnames import unlink_if_ours

    assert unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest.db", "gw3") is False
    assert (sqlite_tree / ".pytest.db").exists(), "the teardown deleted the base database"


def test_a_siblings_file_survives(sqlite_tree):
    """gw3 must not tidy up gw1's database mid-run — the xdist equivalent of the concurrent-run
    corruption GRPH-554 was about."""
    from tests.dbnames import unlink_if_ours

    assert unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest_gw3.db", "gw1") is False
    assert (sqlite_tree / ".pytest_gw3.db").exists()


def test_the_lock_file_is_left_behind_deliberately(sqlite_tree):
    """The one place this does NOT mirror Postgres, pinned so it is not tidied up later.

    Removing the lock opens a race: a second run that has OPENED it but not yet flocked ends
    up holding an unlinked inode, a third creates a fresh file and also succeeds, and two runs
    both believe they hold the claim. The lock being correct matters more than the directory
    being tidy — and the file is empty and gitignored.
    """
    from tests.dbnames import unlink_if_ours

    unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest_gw3.db", "gw3")

    assert (sqlite_tree / ".pytest_gw3.db.lock").exists(), (
        "the lock was deleted — see the docstring; this reintroduces a claim race")


def test_a_serial_run_removes_nothing(sqlite_tree):
    """No worker id means no derived file, and `owns` refuses an empty worker."""
    from tests.dbnames import unlink_if_ours

    assert unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest.db", "") is False
    assert unlink_if_ours(f"sqlite:///{sqlite_tree}/.pytest_gw3.db", "") is False
    assert (sqlite_tree / ".pytest_gw3.db").exists()


@pytest.mark.parametrize("live, suffix, expected", [
    ("./.pytest.db", "_t0", "./.pytest_t0.db"),
    ("./.pytest_gw0.db", "_t1", "./.pytest_gw0_t1.db"),
    ("graphban_test_gw0", "_t0", "graphban_test_gw0_t0"),
])
def test_template_names_keep_sqlite_files_as_db(live, suffix, expected):
    """SQLite snapshots have to stay `*.db` or they escape `backend/*.db` gitignore
    and look like a corrupted sibling of `.pytest.db`."""
    assert template_name(live, suffix) == expected


def test_copy_sqlite_file_replaces_the_target_and_drops_its_wal(tmp_path):
    """A leftover WAL on the target would roll into the snapshot and the next test
    would see a mix of old and new — isolation that looks like a flake."""
    import sqlite3

    source = tmp_path / "src.db"
    target = tmp_path / "dst.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    target.write_bytes(b"old")
    (tmp_path / "dst.db-wal").write_bytes(b"wal")
    (tmp_path / "dst.db-shm").write_bytes(b"shm")
    (tmp_path / "dst.db-journal").write_bytes(b"j")

    copy_sqlite_file(str(source), str(target))

    rows = sqlite3.connect(target).execute("SELECT x FROM t").fetchall()
    assert rows == [(7,)]
    assert not (tmp_path / "dst.db-wal").exists()
    assert not (tmp_path / "dst.db-shm").exists()
    assert not (tmp_path / "dst.db-journal").exists()


def test_the_sqlite_teardown_is_actually_wired_in():
    """GRPH-534's recorded lesson, applied to its own sequel: `owns()` was thoroughly tested
    while the CALL to it was not, and deleting that call broke no test at all.

    So this asserts the wiring rather than the helper — `_drop_worker_database` must reach
    `unlink_if_ours` on SQLite instead of returning early, which is exactly what it did for
    the whole time the files were piling up.
    """
    import inspect

    import conftest

    source = inspect.getsource(conftest._drop_worker_database)
    assert "unlink_if_ours" in source, (
        "the SQLite teardown is not called from _drop_worker_database, so worker files "
        "accumulate again however well the helper is tested")
    assert "if not worker or _is_sqlite():" not in source, (
        "the early return is back — SQLite skips teardown entirely")

    # AND THAT SOMETHING REACHES IT. This is the level my first version missed: the helper
    # was tested, `_drop_worker_database` called it, and `_schema` returned before ever
    # calling `_drop_worker_database` on SQLite — so a real `-n 4` run left all four worker
    # files behind while every test here passed. Exactly GRPH-534's recorded lesson, one
    # level further out than it was recorded at.
    #
    # Both engines now share one session fixture (snapshot restore), so there is no
    # SQLite early-return to slice. The call must still be in `_schema` itself.
    schema = inspect.getsource(conftest._schema)
    assert "_drop_worker_database()" in schema, (
        "_schema no longer drops the worker's database — worker files accumulate again")
    assert "_drop_templates()" in schema, (
        "_schema no longer drops the SQLite snapshot files — they accumulate beside the worker db")


def test_a_test_without_client_sees_an_empty_database():
    """Empty snapshot: tests that never boot the app have always seen no rows.
    Copying the seeded file to them would hand them a prototype they never had."""
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(User)) == 0
    finally:
        db.close()


def test_a_client_test_sees_the_seed(client):
    """Seeded snapshot: the lifespan still calls seed(), which short-circuits on
    the copied rows rather than inserting them again."""
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(User)) > 0
    finally:
        db.close()


def test_sqlite_reset_is_a_file_copy_not_a_delete_loop():
    """THE CALL. SQLite used to DELETE every table and re-seed per test, which is how it
    became the 19-minute CI job after Postgres got TEMPLATE copies. Restoring the delete
    loop would pass every helper test here and put the wall clock back.
    """
    import inspect

    import conftest

    clean = inspect.getsource(conftest._clean_database)
    assert "_reset_data()" not in clean, (
        "SQLite is truncating again — the snapshot restore is what made it cheap")
    assert "_copy_database(" in clean
    schema = inspect.getsource(conftest._schema)
    assert "_build_templates()" in schema, (
        "_schema no longer builds snapshots, so every test pays a full seed again")
    copy = inspect.getsource(conftest._copy_database)
    # THE CALL, not the import. An `import copy_sqlite_file` plus an early return
    # still contains the name, and that is exactly the hole this test exists to
    # close — the helper fully tested, the caller skipping it, the suite green.
    assert "copy_sqlite_file(source, target)" in copy, (
        "_copy_database no longer file-copies on SQLite, so the snapshot path is Postgres-only again")
