"""GRPH-1768: sync_manifest.version is BIGINT.

The column once held version_of()'s signed 63-bit hash. INTEGER is int4 on
Postgres. A value such as 8226209643480881791 raises NumericValueOutOfRange
there, and SQLite stores it without complaint, which is why a green SQLite
suite did not show this. New wire versions fit in 2^53-1 (GRPH-1815). The
column stays BIGINT so a row written under the hash still loads.
sync_outbox.version is a capture counter and stays INTEGER.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import BigInteger, Integer, create_engine, inspect, text
from sqlalchemy.exc import DataError

from app.db import SessionLocal, engine as app_engine
from app.models import SyncManifest, SyncOutbox

# Largest signed bigint. The column must still accept it (GRPH-1768).
WIDE = 2**63 - 1
_BACKEND = pathlib.Path(__file__).parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0213_sync_manifest_version_bigint.py"

IS_PG = app_engine.url.drivername.startswith("postgresql")
postgres_only = pytest.mark.skipif(
    not IS_PG,
    reason=(
        "MANIFEST VERSION WIDTH UNVERIFIED ON THIS ENGINE. SQLite does not enforce "
        "integer width, so an INTEGER column accepts 2**63-1 here and the overflow "
        "stays invisible. CI runs this against Postgres."
    ),
)


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _migration():
    spec = importlib.util.spec_from_file_location("mig_0213", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(engine, fn):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def _declared_type(engine) -> str:
    col = next(c for c in inspect(engine).get_columns("sync_manifest") if c["name"] == "version")
    return str(col["type"]).upper()


def _legacy_sqlite():
    """sync_manifest as migration 0211 created it: version INTEGER."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sync_manifest ("
            "project_id VARCHAR NOT NULL, entity_type VARCHAR NOT NULL, "
            "entity_id VARCHAR NOT NULL, version INTEGER NOT NULL DEFAULT 0, "
            "hash VARCHAR NOT NULL DEFAULT '', confirmed_at DATETIME, "
            "CONSTRAINT pk_sync_manifest PRIMARY KEY (project_id, entity_type, entity_id))"))
        conn.execute(text(
            "INSERT INTO sync_manifest (project_id, entity_type, entity_id, version, hash) "
            "VALUES ('core', 'item', 'GRPH-1', 3, 'sha256:small')"))
    return engine


def test_the_model_is_bigint_and_the_outbox_counter_stays_integer():
    # BigInteger subclasses Integer, so the outbox check is the exact class.
    assert isinstance(SyncManifest.__table__.c.version.type, BigInteger)
    assert type(SyncOutbox.__table__.c.version.type) is Integer
    source = _MIGRATION.read_text()
    assert 'type_=sa.BigInteger()' in source
    assert 'type_=sa.Integer()' in source
    assert 'down_revision: Union[str, None] = "0212"' in source


def test_upgrade_declares_bigint_and_downgrade_restores_integer():
    """The migration's own upgrade and downgrade, on a table shaped like 0211.

    SQLite stores the declared type and does not enforce it. The type name is
    what this asserts. Postgres enforces it in the test below.
    """
    engine = _legacy_sqlite()
    mod = _migration()
    _run(engine, mod.upgrade)
    assert "BIGINT" in _declared_type(engine)
    with engine.connect() as conn:
        kept = conn.execute(text(
            "SELECT version, hash FROM sync_manifest WHERE entity_id = 'GRPH-1'")).one()
    assert kept == (3, "sha256:small")

    _run(engine, mod.downgrade)
    assert _declared_type(engine) == "INTEGER"
    with engine.connect() as conn:
        kept = conn.execute(text(
            "SELECT version FROM sync_manifest WHERE entity_id = 'GRPH-1'")).one()
    assert kept[0] == 3


@postgres_only
def test_a_migrated_postgres_accepts_the_max_signed_manifest_version(db):
    """The column the app is running, after Alembic upgrade, not a side schema."""
    got = db.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'sync_manifest' "
        "AND column_name = 'version'")).scalar()
    assert got == "bigint"
    db.add(SyncManifest(
        project_id="p1768", entity_type="item", entity_id="wide",
        version=WIDE, hash="sha256:wide"))
    db.commit()
    db.expire_all()
    row = db.get(SyncManifest, ("p1768", "item", "wide"))
    assert row is not None
    assert row.version == WIDE


@postgres_only
def test_postgres_downgrade_restores_integer_and_refuses_the_wide_value():
    """Downgrade is not a no-op: the restored INTEGER rejects 2**63-1."""
    from tests.conftest import _create_database

    base, _, _ = os.environ["DATABASE_URL"].rpartition("/")
    name = f"gb_mig1768_{uuid.uuid4().hex[:8]}"
    _create_database(f"{base}/postgres", name)
    url = f"{base}/{name}"
    engine = create_engine(url)
    mod = _migration()
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE sync_manifest ("
                "project_id VARCHAR NOT NULL, entity_type VARCHAR NOT NULL, "
                "entity_id VARCHAR NOT NULL, version INTEGER NOT NULL DEFAULT 0, "
                "hash VARCHAR NOT NULL DEFAULT '', "
                "confirmed_at TIMESTAMP WITH TIME ZONE, "
                "CONSTRAINT pk_sync_manifest PRIMARY KEY "
                "(project_id, entity_type, entity_id))"))
            conn.execute(text(
                "INSERT INTO sync_manifest "
                "(project_id, entity_type, entity_id, version, hash) "
                "VALUES ('core', 'item', 'GRPH-1', 3, 'sha256:small')"))
        _run(engine, mod.upgrade)
        with engine.begin() as conn:
            kind = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'sync_manifest' AND column_name = 'version'"
            )).scalar()
            assert kind == "bigint"
            conn.execute(text(
                "INSERT INTO sync_manifest "
                "(project_id, entity_type, entity_id, version, hash) "
                "VALUES ('core', 'item', 'wide', :v, 'sha256:wide')"), {"v": WIDE})
            stored = conn.execute(text(
                "SELECT version FROM sync_manifest WHERE entity_id = 'wide'")).scalar()
            assert stored == WIDE
            conn.execute(text("DELETE FROM sync_manifest WHERE entity_id = 'wide'"))
        _run(engine, mod.downgrade)
        with engine.begin() as conn:
            kind = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'sync_manifest' AND column_name = 'version'"
            )).scalar()
            assert kind == "integer"
            kept = conn.execute(text(
                "SELECT version FROM sync_manifest WHERE entity_id = 'GRPH-1'")).scalar()
            assert kept == 3
        with pytest.raises(DataError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO sync_manifest "
                    "(project_id, entity_type, entity_id, version, hash) "
                    "VALUES ('core', 'item', 'wide', :v, 'sha256:wide')"), {"v": WIDE})
    finally:
        engine.dispose()
        admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            admin.dispose()
