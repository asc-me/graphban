"""Shared fixtures.

**The schema is built once per session; only the DATA is reset between tests.**

It used to be rebuilt per test — `drop_all` plus a dropped `alembic_version`, so the
lifespan re-ran the entire migration chain for every one of ~1,400 tests. Measured on
Postgres that was 231 ms each, and setup accounted for essentially the whole 13-minute CI
job; the test bodies were close to free. Truncating instead costs ~97 ms and takes the job
to roughly half.

What that buys is only worth having if isolation survives it, so:

- every table is emptied before each test, so no test can see another's rows;
- the schema is re-checked before each test too (a 14 ms no-op at head). Two tests
  deliberately DOWNGRADE the schema mid-run to prove a data migration backfills real rows,
  and one of them failing part-way would otherwise leave every later test running against
  a schema from revision 0037.

Seeding still runs per test, inside the app's own lifespan — ~198 ms, and the largest cost
left. Hoisting it needs each test wrapped in a transaction that is rolled back, which the
downgrade tests above cannot live inside, so it is deliberately not done here.
"""
import os

# Must be set before app modules import settings. setdefault so CI can point the
# suite at Postgres (DATABASE_URL=postgresql+psycopg://...) to exercise the real
# Alembic chain and pgvector `<=>` search path; local runs default to SQLite.
os.environ.setdefault("DATABASE_URL", "sqlite:///./.pytest.db")
os.environ["SEED_ON_START"] = "true"
# The credential retry loop is OFF for the suite (PRD-25 S2b). A background task that fires
# mid-test turns an unrelated assertion into a flake, and the loop's own tests drive
# `run_once` directly rather than waiting on a timer — so nothing is lost by silencing it and
# a whole class of intermittent failure is avoided. `test_credential_retry_loop.py` turns it
# back on explicitly for the tests that are ABOUT the task.
os.environ["CREDENTIAL_RETRY_SECONDS"] = "0"


def _database_per_worker() -> None:
    """Give each xdist worker its own database (GRPH-353).

    Not optional under `-n`. The suite empties every table between tests, so workers
    sharing one database would truncate each other's rows mid-test — and the failures would
    land on whichever test happened to be reading at the time, nowhere near the cause.

    A no-op without xdist, so a serial run is byte-for-byte what it was.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")  # "gw0", "gw1", … ; unset when serial
    if not worker:
        return

    from tests.dbnames import worker_url

    url = os.environ["DATABASE_URL"]
    # Idempotent. This module rewrites the environment at IMPORT time, and pytest can load
    # it more than once — it happened, and produced `graphban_test_gw0_gw0` plus eighteen
    # stray databases without a single test failing, because the engine was already bound
    # to the first name. Cheap to make re-entrant; expensive to notice when it is not.
    if url.endswith(f"_{worker}") or url.endswith(f"_{worker}.db"):
        return

    os.environ["DATABASE_URL"] = worker_url(url, worker)
    if not url.startswith("sqlite"):
        base, _, _ = url.rpartition("/")
        _, _, name = worker_url(url, worker).rpartition("/")
        _create_database(f"{base}/postgres", name)


def _create_database(admin_url: str, name: str) -> None:
    """`CREATE DATABASE` on the same server, ignoring "already exists".

    Talks to `postgres` rather than the target, because you cannot create a database from
    inside the one you are creating. Autocommit because CREATE DATABASE cannot run in a
    transaction.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("select 1 from pg_database where datname = :n"), {"n": name}
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


_database_per_worker()

import pytest
from fastapi.testclient import TestClient

from tests import schema_probe


# ---- outputSchema conformance (GRPH-495) ---------------------------------------------
#
# Split across three hooks because the observation happens in workers and the verdict has to
# be reached in the controller — the same shape pytest-cov uses to combine coverage data.
# See tests/schema_probe.py for what is being checked and why it is observed rather than
# read statically.


def pytest_configure(config):
    # The controller mints the directory; xdist workers inherit it through the environment
    # when execnet spawns them. `setdefault` is what makes the worker keep the inherited
    # value instead of minting a second one nobody reads.
    os.environ.setdefault(schema_probe.ENV_DIR, schema_probe.make_dir())
    schema_probe.install()


def _is_full_run(config) -> bool:
    """Was this the whole suite, or a selection?

    Only a full run can demand that every tool was exercised. Running one file must not
    fail because the other fifty tools did not happen — that would make the ratchet
    something people learn to ignore, which is worse than not having it.
    """
    if config.option.keyword or config.option.markexpr:
        return False
    ini = config.getini("testpaths") or []
    return all(arg in ("", ".", *ini) for arg in config.args)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if hasattr(config, "workerinput"):   # an xdist worker: record and let the controller judge
        schema_probe.dump()
        return
    schema_probe.dump()                  # serial run: this process is both halves
    failures = schema_probe.report(_is_full_run(config))
    if failures:
        config._gb_schema_failures = failures
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    failures = getattr(config, "_gb_schema_failures", None)
    if failures:
        terminalreporter.section("outputSchema conformance (GRPH-495)", red=True, bold=True)
        for line in failures:
            terminalreporter.write_line(line)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    # The spam limiter keeps in-process state; clear it so tests don't leak counts.
    from app.services import spam

    spam._hits.clear()
    yield


def _is_sqlite() -> bool:
    from app.db import engine

    return engine.url.drivername.startswith("sqlite")


def _build_schema() -> None:
    """Bring the database to head. A no-op once it is there (~14 ms on Postgres).

    Also the repair path: a test that downgrades and fails before upgrading back leaves the
    schema behind, and every later test would fail somewhere far from the cause.
    """
    if _is_sqlite():
        from app.db import init_db

        init_db()
    else:
        from app.migrate import run_migrations

        run_migrations()


# Postgres restores each test from a TEMPLATE database rather than emptying the live one
# (GRPH-529). Two templates per worker, because the two are genuinely different states:
_TMPL_EMPTY = "_t0"    # schema at head, no rows — what a test WITHOUT `client` sees today
_TMPL_SEEDED = "_t1"   # schema at head, prototype dataset loaded


def _admin_engine():
    """A connection to `postgres`, because you cannot DROP the database you are in."""
    import sqlalchemy as sa

    from app.db import engine

    return sa.create_engine(engine.url.set(database="postgres"),
                            isolation_level="AUTOCOMMIT")


def _copy_database(source: str, target: str) -> None:
    """`CREATE DATABASE target TEMPLATE source` — a file copy, and in CI a RAM copy since
    the cluster lives in tmpfs.

    Postgres refuses to copy a database anyone is connected to, and refuses to drop one
    too, so both ends are cleared first. `engine.dispose()` handles our own pool; the
    terminate covers a connection some test opened and did not close, which would otherwise
    surface as an unexplained failure in whichever test happened to run next.
    """
    from sqlalchemy import text

    from app.db import engine

    engine.dispose()
    admin = _admin_engine()
    try:
        with admin.begin() as conn:
            for name in (source, target):
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{target}"')
            conn.exec_driver_sql(f'CREATE DATABASE "{target}" TEMPLATE "{source}"')
    finally:
        admin.dispose()


def _build_templates() -> None:
    """Build both templates once per worker: migrate, snapshot, seed, snapshot.

    The seed used to run inside the app's lifespan on EVERY test — this conftest's own
    docstring called it "the largest cost left", at ~198 ms, and recorded that hoisting it
    needed a per-test transaction the downgrade tests cannot live inside. A template needs
    no transaction: the seeded rows are already in the file being copied, and `seed()`
    short-circuits on `select(User).limit(1)`, so the lifespan's call becomes one SELECT.
    """
    from app.db import SessionLocal, engine
    from app.seed import seed

    live = engine.url.database
    _build_schema()
    _copy_database(live, live + _TMPL_EMPTY)
    db = SessionLocal()
    try:
        seed(db)          # commits
    finally:
        db.close()
    _copy_database(live, live + _TMPL_SEEDED)


def _drop_templates() -> None:
    """Leave nothing behind. A worker that dies mid-session leaves its templates, and the
    next session's `CREATE DATABASE` would fail on a name already taken — so the build side
    drops before creating too, and this is the tidy path rather than the only one."""
    from sqlalchemy import text

    from app.db import engine

    name = engine.url.database
    engine.dispose()
    admin = _admin_engine()
    try:
        with admin.begin() as conn:
            for suffix in (_TMPL_EMPTY, _TMPL_SEEDED):
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name + suffix})
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name + suffix}"')
    finally:
        admin.dispose()


def _drop_schema() -> None:
    from sqlalchemy import text

    from app.db import Base, engine

    Base.metadata.drop_all(engine)
    if not _is_sqlite():
        # alembic_version isn't in Base.metadata; drop it too or a later `upgrade head`
        # would think the (now-dropped) schema is still current.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _reset_data() -> None:
    """Empty every table, leaving the schema alone.

    `alembic_version` is untouched because it is not in `Base.metadata` — emptying it would
    put the next migration check back to square one, which is the cost being removed.
    """
    from sqlalchemy import text

    from app.db import Base, engine

    tables = Base.metadata.sorted_tables
    with engine.begin() as conn:
        if _is_sqlite():
            # No TRUNCATE in SQLite. Reverse dependency order so a FK never blocks a delete.
            for table in reversed(tables):
                conn.execute(table.delete())
        else:
            names = ", ".join(f'"{t.name}"' for t in tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema once for the whole session, and leave nothing behind."""
    _drop_schema()  # a previous run may have died mid-test
    if _is_sqlite():
        _build_schema()
        yield
        _drop_schema()
        return
    _build_templates()
    yield
    _drop_schema()
    _drop_templates()


@pytest.fixture(autouse=True)
def _clean_database(_schema, request):
    """Every test starts against a known database — not only the ones asking for `client`.

    Autouse because the alternative is an invariant held by convention. Before the schema
    was hoisted, a test that reached the database WITHOUT `client` found no tables at all
    and failed loudly; now it would quietly read whatever the previous test left, which is
    the kind of thing that surfaces as an unexplained flake months later.

    On Postgres this is a TEMPLATE copy rather than a truncate-and-reseed (GRPH-529):
    ~80 ms against 358, because the seeded rows arrive in the file copy instead of being
    INSERTed again for every test.

    **Which template depends on what the test asked for**, read off its own fixture
    closure. A test that uses `client` gets the app's lifespan, which seeds — so it has
    always seen the prototype dataset. A test that does not has always seen an empty
    database. Copying the seeded template to both would hand ~109 tests rows they have
    never had, and the ones that break would break in ways unrelated to what they test.

    The copy also subsumes the schema repair the old path needed: a test that downgrades
    and fails before upgrading back used to leave the schema behind for everything after
    it. The next test does not repair that schema, it replaces the whole database.
    """
    if _is_sqlite():
        _build_schema()  # no-op at head; repairs after a test that downgraded
        _reset_data()
        yield
        return
    from app.db import engine

    live = engine.url.database
    seeded = "client" in request.fixturenames
    _copy_database(live + (_TMPL_SEEDED if seeded else _TMPL_EMPTY), live)
    yield


@pytest.fixture()
def client(_clean_database):
    """Depends on the reset EXPLICITLY rather than trusting autouse ordering — the app's
    lifespan seeds on startup, and a reset that ran afterwards would wipe the seed."""
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    r = client.post(
        "/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def decoy(client):
    """A populated SECOND project, for any test asserting that a read is project-scoped.

    Depends on `client` for the same reason every `db` fixture here does: the app's lifespan
    seeds on startup, so a session opened before it has no user to own a project.

    Opens its own session and commits, so a suite's own `db` fixture sees the rows without
    the two sharing identity map state. See `tests/decoy.py` for why this exists (GRPH-436).
    """
    from app.db import SessionLocal

    from tests.decoy import seed_decoy

    s = SessionLocal()
    try:
        yield seed_decoy(s)
    finally:
        s.close()
