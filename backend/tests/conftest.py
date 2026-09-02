"""Shared fixtures.

**The schema is built once per session. Each test is a snapshot restore, not a truncate.**

It used to be rebuilt per test — `drop_all` plus a dropped `alembic_version`, so the
lifespan re-ran the entire migration chain for every test. Truncating instead cut that
roughly in half, and seeding (~198 ms, inside the app's lifespan) was then the largest
cost left. Hoisting the seed needed a per-test transaction the downgrade tests cannot
live inside, so it stayed.

GRPH-529 hoisted it anyway, with a TEMPLATE copy on Postgres: two snapshots per worker
(empty / seeded), restored by replacing the live database. SQLite did not get that path
and kept DELETE-every-table plus a re-seed. The suite grew from ~1,400 tests to 3,420;
SQLite CI paid the old reset 3,420 times (~19 min) while Postgres paid a file copy
(~7 min). This file now uses the snapshot restore on both engines — `CREATE DATABASE
… TEMPLATE …` on Postgres, a file copy on SQLite.

Which snapshot depends on what the test asked for. A test that uses `client` has always
seen the prototype dataset (the lifespan seeds). A test that does not has always seen
an empty database. The copy also subsumes schema repair: a test that downgrades and
fails used to leave the schema behind; the next test replaces the whole database.
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

    from tests.dbnames import refuse_if_in_use, worker_url

    url = os.environ["DATABASE_URL"]
    # Idempotent. This module rewrites the environment at IMPORT time, and pytest can load
    # it more than once — it happened, and produced `graphban_test_gw0_gw0` plus eighteen
    # stray databases without a single test failing, because the engine was already bound
    # to the first name. Cheap to make re-entrant; expensive to notice when it is not.
    if url.endswith(f"_{worker}") or url.endswith(f"_{worker}.db"):
        return

    isolated = worker_url(url, worker)
    if isolated == url:
        # REFUSE RATHER THAN NO-OP (GRPH-568). An unchanged URL means every xdist worker shares
        # one database and they race to create the schema — measured at 2,659
        # `table users already exists` errors, loud and pointing nowhere near the cause. The
        # previous derivation did this silently for any sqlite name that was not the default,
        # which broke the escape hatch GRPH-554's own lock message recommends.
        from tests.dbnames import NotIsolated

        raise NotIsolated(
            f"{url!r} did not yield a per-worker database for {worker!r}, so every worker "
            "would share it. Run serially, or use a DATABASE_URL whose final path segment can "
            "carry a worker suffix."
        )
    os.environ["DATABASE_URL"] = isolated
    if not url.startswith("sqlite"):
        base, _, _ = url.rpartition("/")
        _, _, name = worker_url(url, worker).rpartition("/")
        # Before adopting it, and before this process connects — so any backend on that
        # database belongs to another run (GRPH-534).
        refuse_if_in_use(f"{base}/postgres", name)
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

# CLAIM THE SQLITE FILE, after the per-worker rewrite has settled DATABASE_URL and before
# anything connects (GRPH-554). Postgres got this guard in GRPH-534; SQLite did not, so two
# runs in one working tree shared one file — and because the suite deletes and rebuilds that
# file, one run unlinks the database the other has open. It surfaces as "no such table",
# "readonly database", "malformed database schema" or a UNIQUE violation in the seed, none of
# which name the cause. Refusing costs one clear error; not refusing cost this repository a
# recurring mystery.
from tests.dbnames import claim_sqlite  # noqa: E402

claim_sqlite(os.environ["DATABASE_URL"])
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
    """Replace `target` with a copy of `source`.

    Postgres: `CREATE DATABASE target TEMPLATE source` — a file copy, and in CI a RAM
    copy since the cluster lives in tmpfs. It refuses to copy a database anyone is
    connected to, so both ends are cleared first. `engine.dispose()` handles our own
    pool; the terminate covers a connection some test opened and did not close.

    SQLite: the same idea with no server — copy the file. The caller has disposed the
    pool; leftover WAL/journal on the target is dropped so it cannot roll into the
    snapshot.
    """
    from app.db import engine

    engine.dispose()
    if _is_sqlite():
        from tests.dbnames import copy_sqlite_file

        copy_sqlite_file(source, target)
        return

    from sqlalchemy import text

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

    from tests.dbnames import template_name

    live = engine.url.database
    _build_schema()
    _copy_database(live, template_name(live, _TMPL_EMPTY))
    db = SessionLocal()
    try:
        seed(db)          # commits
    finally:
        db.close()
    _copy_database(live, template_name(live, _TMPL_SEEDED))


def _drop_templates() -> None:
    """Leave nothing behind. A worker that dies mid-session leaves its templates, and the
    next session's restore would fail on a name already taken — so the build side
    drops before creating too, and this is the tidy path rather than the only one."""
    from app.db import engine
    from tests.dbnames import template_name, unlink_sqlite_path

    live = engine.url.database
    engine.dispose()
    if _is_sqlite():
        for suffix in (_TMPL_EMPTY, _TMPL_SEEDED):
            unlink_sqlite_path(template_name(live, suffix))
        return

    from sqlalchemy import text

    admin = _admin_engine()
    try:
        with admin.begin() as conn:
            for suffix in (_TMPL_EMPTY, _TMPL_SEEDED):
                name = template_name(live, suffix)
                conn.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.dispose()


def _drop_worker_database() -> None:
    """Drop the database this worker created, at the end of its session (GRPH-534).

    Nothing used to remove these: one developer machine had 76, 771 MB, four base names
    across eighteen workers, from every parallel run ever done against that container. In CI
    the job is ephemeral so it never showed.

    The decision and the DROP both live in `tests.dbnames.drop_if_ours`, which is importable
    without side effects and therefore testable. That split is not tidiness — this is the
    destructive path, and the first version kept the check inline here: `owns()` was
    thoroughly tested, and deleting the call to it from this function broke no test at all.
    The sabotage pass caught exactly that.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return
    from app.db import engine

    url = engine.url.render_as_string(hide_password=False)
    engine.dispose()

    if _is_sqlite():
        # SQLITE USED TO RETURN EARLY HERE, and its worker FILES accumulated exactly the way
        # the Postgres databases did before GRPH-534 — eighteen of them in one working tree.
        # The early return was correct about the reason (there is no server to DROP on) and
        # wrong to stop there, which GRPH-554's acceptance said and GRPH-554 did not do.
        from tests.dbnames import unlink_if_ours

        unlink_if_ours(url, worker)
        return

    from tests.dbnames import drop_if_ours

    drop_if_ours(url, worker)


def _drop_schema() -> None:
    from sqlalchemy import text

    from app.db import Base, engine

    Base.metadata.drop_all(engine)
    if not _is_sqlite():
        # alembic_version isn't in Base.metadata; drop it too or a later `upgrade head`
        # would think the (now-dropped) schema is still current.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema once for the whole session, and leave nothing behind.

    Both engines restore from snapshots now, so the session fixture is one path:
    drop whatever a previous run left, build empty+seeded templates, yield, then
    drop templates and the worker's own database. SQLite used to return before
    `_drop_worker_database` — a `-n 4` run left all four `.pytest_gw*.db` behind
    with the helper fully tested and correctly called from a function nothing reached.
    """
    _drop_schema()  # a previous run may have died mid-test
    _build_templates()
    yield
    _drop_schema()
    _drop_templates()
    _drop_worker_database()


@pytest.fixture(autouse=True)
def _clean_database(_schema, request):
    """Every test starts against a known database — not only the ones asking for `client`.

    Autouse because the alternative is an invariant held by convention. Before the schema
    was hoisted, a test that reached the database WITHOUT `client` found no tables at all
    and failed loudly; now it would quietly read whatever the previous test left, which is
    the kind of thing that surfaces as an unexplained flake months later.

    This is a snapshot restore rather than a truncate-and-reseed (GRPH-529): the seeded
    rows arrive in the file copy instead of being INSERTed again for every test. SQLite
    used to DELETE every table and re-seed; that is the 19-minute CI job.

    **Which template depends on what the test asked for**, read off its own fixture
    closure. A test that uses `client` gets the app's lifespan, which seeds — so it has
    always seen the prototype dataset. A test that does not has always seen an empty
    database. Copying the seeded template to both would hand ~109 tests rows they have
    never had, and the ones that break would break in ways unrelated to what they test.

    The copy also subsumes the schema repair the old path needed: a test that downgrades
    and fails before upgrading back used to leave the schema behind for everything after
    it. The next test does not repair that schema, it replaces the whole database.
    """
    from app.db import engine
    from tests.dbnames import template_name

    live = engine.url.database
    seeded = "client" in request.fixturenames
    _copy_database(template_name(live, _TMPL_SEEDED if seeded else _TMPL_EMPTY), live)
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
