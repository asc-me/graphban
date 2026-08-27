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

from tests.dbnames import drop_if_ours, owns, refuse_if_in_use, worker_url


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
