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

import pytest
from fastapi.testclient import TestClient


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
    _build_schema()
    yield
    _drop_schema()


@pytest.fixture()
def client():
    from app.main import app

    _build_schema()  # no-op at head; repairs after a test that downgraded
    _reset_data()  # start clean — the lifespan re-seeds into the empty tables
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    r = client.post(
        "/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
