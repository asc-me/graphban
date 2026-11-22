"""GRPH-1850: a project that is not at sync does not report a full-sync backfill.

Leaving sync clears the manifest. ``backfill_status`` then reads every current
entity as unconfirmed and answers ``running``, confirmed 0 of N. That number
cannot move. The route must not return it.

Returning ``backfill_status`` unchanged from ``project_view`` for a local
project fails ``test_local_with_purge_reports_backfill_off_not_running``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import SyncManifest, SyncProject, User
from app.services import items as items_svc
from app.services import project_sync_backfill as backfill
from app.services import project_sync_flush as flush
from app.services import projects as projects_svc
from tests.project_sync_fake import FakeConsole
from tests.test_project_sync_api import (
    TOKEN,
    _bind_transport,
    _link,
    _local,
    _stub_console,
)

LATER = datetime(2099, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def db(client):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _admin(db) -> User:
    return db.scalars(select(User).where(User.email == "alex@ascme-labs.com")).one()


def _fresh(monkeypatch) -> FakeConsole:
    fake = FakeConsole()
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    _bind_transport(monkeypatch, fake)
    _stub_console(monkeypatch)
    return fake


def _project(db, tag: str, name: str):
    project = projects_svc.create_project(
        db, name=name, owner_user_id=_admin(db).id, tag=tag,
    )
    items_svc.create_item(db, project_id=project.id, title="one", status="next")
    items_svc.create_item(db, project_id=project.id, title="two", status="next")
    return project


def _put(client, auth, project_id: str, body: dict):
    saved = client.put(f"/api/sync/projects/{project_id}", headers=auth, json=body)
    assert saved.status_code == 200, saved.text
    return saved.json()


def _status(client, auth, project_id: str) -> dict:
    body = client.get("/api/sync/projects", headers=auth).json()
    return _local(body, project_id)


def _raw(project_id: str) -> dict:
    with SessionLocal() as check:
        return backfill.backfill_status(check, project_id)


def _manifest_rows(project_id: str) -> int:
    with SessionLocal() as check:
        return check.scalar(
            select(func.count()).select_from(SyncManifest).where(
                SyncManifest.project_id == project_id))


def test_local_with_purge_reports_backfill_off_not_running(client, auth, db, monkeypatch):
    """The cleared manifest is running, 0 of N. The route must not say so."""
    _fresh(monkeypatch)
    _link(db)
    project = _project(db, "SLOC", "Local purge")
    started = _put(client, auth, project.id, {"sync_level": "sync", "create_in_org": True})
    assert started["project"]["backfill"]["state"] == "running"
    assert started["project"]["backfill"]["confirmed"] == 0
    assert started["project"]["backfill"]["total"] >= 2

    saved = _put(client, auth, project.id, {"sync_level": "local", "purge": True})
    assert saved["project"]["sync_level"] == "local"
    assert saved["project"]["backfill"] == {"state": "off"}

    row = _status(client, auth, project.id)
    assert row["sync_level"] == "local"
    assert row["backfill"] == {"state": "off"}
    # The reset manifest is still a running backfill if anyone returns it as-is.
    raw = _raw(project.id)
    assert raw["state"] == "running"
    assert raw["confirmed"] == 0
    assert raw["total"] >= 2
    assert _manifest_rows(project.id) == 0


def test_local_without_purge_reports_backfill_off(client, auth, db, monkeypatch):
    """The kept manifest would read as done. Local only is still not a backfill."""
    fake = _fresh(monkeypatch)
    _link(db)
    project = _project(db, "SLOP", "Local keep")
    _put(client, auth, project.id, {"sync_level": "sync", "create_in_org": True})
    flush.reset_runtime()
    try:
        with SessionLocal() as fresh:
            assert flush.flush_once(
                fresh, now=LATER, transport=fake.transport(),
                state=flush.FlushState(), rng=lambda: 0,
            ) >= 1
    finally:
        flush.reset_runtime()
    assert _raw(project.id)["state"] == "done"
    kept = _manifest_rows(project.id)
    assert kept >= 2

    saved = _put(client, auth, project.id, {"sync_level": "local", "purge": False})
    assert saved["project"]["backfill"] == {"state": "off"}
    row = _status(client, auth, project.id)
    assert row["sync_level"] == "local"
    assert row["backfill"] == {"state": "off"}
    assert _manifest_rows(project.id) == kept
    assert _raw(project.id)["state"] == "done"


def test_digests_reports_no_full_sync_backfill(client, auth, db, monkeypatch):
    _fresh(monkeypatch)  # the list route still reads the console
    _link(db)
    project = _project(db, "SDIG", "Digests only")
    saved = _put(client, auth, project.id, {
        "sync_level": "digests", "cloud_project_id": "cloud-dig", "cloud_project_tag": "SDIG",
    })
    assert saved["project"]["sync_level"] == "digests"
    assert saved["project"]["backfill"] == {"state": "off"}
    assert "total" not in saved["project"]["backfill"]
    row = _status(client, auth, project.id)
    assert row["sync_level"] == "digests"
    assert row["backfill"] == {"state": "off"}
    raw = _raw(project.id)
    assert raw["state"] == "running" and raw["total"] >= 2


def test_returning_to_sync_runs_a_fresh_backfill_then_done(client, auth, db, monkeypatch):
    fake = _fresh(monkeypatch)
    _link(db)
    project = _project(db, "SBAK", "Back on")
    _put(client, auth, project.id, {"sync_level": "sync", "create_in_org": True})
    with SessionLocal() as check:
        cloud_id = check.get(SyncProject, project.id).cloud_project_id
    assert cloud_id
    _put(client, auth, project.id, {"sync_level": "local", "purge": True})
    assert _status(client, auth, project.id)["backfill"] == {"state": "off"}

    again = _put(client, auth, project.id, {
        "sync_level": "sync", "cloud_project_id": cloud_id, "cloud_project_tag": "SBAK",
    })
    running = again["project"]["backfill"]
    assert running["state"] == "running"
    assert running["confirmed"] == 0
    assert running["total"] >= 2
    assert _status(client, auth, project.id)["backfill"]["state"] == "running"

    flush.reset_runtime()
    try:
        with SessionLocal() as fresh:
            assert flush.flush_once(
                fresh, now=datetime.now(timezone.utc) + timedelta(minutes=2),
                transport=fake.transport(), state=flush.FlushState(), rng=lambda: 0,
            ) >= 1
    finally:
        flush.reset_runtime()
    done = _status(client, auth, project.id)["backfill"]
    assert done["state"] == "done"
    assert done["confirmed"] == done["total"]
    assert done["total"] >= 2
