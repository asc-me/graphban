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
