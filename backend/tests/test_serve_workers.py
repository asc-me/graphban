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


# --- the log-export panel answers for the leader, not for the worker it lands on --------

from app.services import logexport  # noqa: E402
from tests.test_logexport import _enable, db  # noqa: E402, F401


def test_sqlite_has_no_leader_elsewhere(db, monkeypatch):
    monkeypatch.setattr(leader.settings, "database_url", "sqlite:///x.db")
    assert leader.leader_lock_held(db.connection()) is False


@_pg
def test_a_non_leader_worker_reports_the_leaders_exporter(db, monkeypatch):
    """This session holds no lock and runs no loop; another session (the leader worker) holds
    the leader lock. The panel must say "exporting", not "not running"."""
    _enable(db)
    leader.release_background_leader()  # this process is the non-leader worker
    logexport.mark_exporter_stopped()
    monkeypatch.setattr(logexport.settings, "log_export_seconds", 60)

    out = logexport.status(db)
    assert out["state"] == "not_running"  # nobody holds the lock: nothing is draining
    assert out["queue_depth"] is None

    other = create_engine(leader.settings.database_url)
    try:
        with other.connect() as conn:
            assert conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                {"k": leader._LEADER_KEY}).scalar() is True
            assert leader.leader_lock_held(db.connection()) is True
            out = logexport.status(db)
            assert out["state"] == "exporting"
            assert out["exporter_running"] is True
            assert isinstance(out["queue_depth"], int)

            # The leader runs no exporter when the loop is disabled; holding the lock is not enough.
            monkeypatch.setattr(logexport.settings, "log_export_seconds", 0)
            assert logexport.status(db)["state"] == "not_running"
            monkeypatch.setattr(logexport.settings, "log_export_seconds", 60)

            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": leader._LEADER_KEY})
            conn.commit()
        assert logexport.status(db)["state"] == "not_running"  # leader gone: say so
    finally:
        other.dispose()


def test_a_failed_presence_read_is_named_not_read_as_draining(db, monkeypatch):
    _enable(db)
    logexport.mark_exporter_stopped()
    monkeypatch.setattr(logexport.settings, "log_export_seconds", 60)

    def explode(_conn):
        raise RuntimeError("pg_locks unreadable")

    monkeypatch.setattr(leader, "leader_lock_held", explode)
    out = logexport.status(db)
    assert out["exporter_running"] is False
    assert out["queue_depth"] is None
    assert out["coverage"] == "unavailable"
    assert "exporter presence" in out["note"]
