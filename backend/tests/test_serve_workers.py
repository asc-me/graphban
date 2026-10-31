"""serve.py runs N uvicorn workers, SQLite stays at one (GRPH-1241)."""
from __future__ import annotations

from unittest import mock

from app import leader, serve


def _run_main(monkeypatch, **overrides):
    for k, v in overrides.items():
        monkeypatch.setattr(serve.settings, k, v, raising=False)
    with mock.patch.object(serve.uvicorn, "run") as run, \
            mock.patch.object(serve, "configure_logging"):
        serve.main()
    return run.call_args.kwargs


def test_workers_come_from_settings_on_postgres(monkeypatch):
    kw = _run_main(monkeypatch, database_url="postgresql+psycopg://u:p@h/db", web_concurrency=3)
    assert kw["workers"] == 3
    assert kw["log_config"] is None


def test_default_is_min_of_four_and_cpu_count(monkeypatch):
    monkeypatch.setattr(serve.os, "cpu_count", lambda: 16)
    kw = _run_main(monkeypatch, database_url="postgresql+psycopg://u:p@h/db", web_concurrency=0)
    assert kw["workers"] == 4
    monkeypatch.setattr(serve.os, "cpu_count", lambda: 2)
    kw = _run_main(monkeypatch, database_url="postgresql+psycopg://u:p@h/db", web_concurrency=0)
    assert kw["workers"] == 2


def test_sqlite_forces_one_worker_and_says_so(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="graphban.serve"):
        kw = _run_main(monkeypatch, database_url="sqlite:///x.db", web_concurrency=4)
    assert kw["workers"] == 1
    assert "SQLite" in caplog.text


def test_sqlite_leader_helpers_are_noops(monkeypatch):
    monkeypatch.setattr(leader.settings, "database_url", "sqlite:///x.db")
    with leader.boot_lock():
        pass
    assert leader.claim_background_leader() is True


# --- Postgres: the advisory locks themselves -------------------------------------------
# A second engine is a second session, which is what a second uvicorn worker is.

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

_pg = pytest.mark.skipif(leader.settings.is_sqlite, reason="advisory locks are Postgres-only")


@_pg
def test_only_one_session_wins_the_background_leader():
    other = create_engine(leader.settings.database_url)
    try:
        assert leader.claim_background_leader() is True
        with other.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": leader._LEADER_KEY}).scalar()
            assert got is False  # a second worker loses
        leader.release_background_leader()
        with other.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": leader._LEADER_KEY}).scalar()
            assert got is True  # released, so a successor can take over
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": leader._LEADER_KEY})
    finally:
        leader.release_background_leader()
        other.dispose()


@_pg
def test_boot_lock_excludes_other_sessions_while_held():
    other = create_engine(leader.settings.database_url)
    try:
        with leader.boot_lock():
            with other.connect() as conn:
                got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": leader._BOOT_KEY}).scalar()
                assert got is False  # a second worker would wait here
        with other.connect() as conn:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": leader._BOOT_KEY}).scalar()
            assert got is True
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": leader._BOOT_KEY})
    finally:
        other.dispose()
