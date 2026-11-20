"""GRPH-1739: box API for per-project sync level, permission, queue and budget.

A non-admin is refused on every write. Dropping the admin check on
``PUT /api/sync/projects/{id}`` turns that refusal into a 422 for a bad level,
which fails ``test_a_non_admin_is_refused_on_every_write``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import SyncLink, SyncOutbox, SyncProject
from app.security import secrets
from app.services import items as items_svc
from app.services.project_sync_client import CONTRACT
from app.services import project_sync_client as client_mod
from app.services import project_sync_flush as flush
from app.services import project_sync_outbox as outbox
from tests.project_sync_fake import FakeConsole

CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_API_TOKEN_MUST_NOT_LEAK_7f3a"
BEA = "gb_dep_PROJECT_SYNC_API_BEA_TOKEN_MUST_NOT_LEAK_7f3a"
FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(client):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _link(db, token=TOKEN):
    db.add(SyncLink(
        id="instance", console_url=CONSOLE, cloud_url=CONSOLE, token_enc=secrets.encrypt(token),
    ))
    db.commit()


def _stub_console(monkeypatch, body=None):
    payload = {"projects": []} if body is None else body

    def _get(url, headers=None, timeout=None, **kw):
        return httpx.Response(200, json=payload, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx, "get", _get)


def _bind_transport(monkeypatch, fake: FakeConsole):
    real = client_mod._call

    def wrapped(db, method, path, **kwargs):
        kwargs["transport"] = fake.transport()
        return real(db, method, path, **kwargs)

    monkeypatch.setattr(client_mod, "_call", wrapped)


def _local(body, project_id="core"):
    rows = body["local"]
    return next(row for row in rows if row["project_id"] == project_id)


def test_get_reports_level_queue_and_unknown_as_unknown(client, auth, db, monkeypatch):
    _link(db)
    _stub_console(monkeypatch)
    db.add(SyncProject(
        local_project_id="core", cloud_project_id="cloud-1", sync_level="sync",
        cloud_permission="comments", last_push_result="unknown", last_push_reason="429",
    ))
    db.add(SyncOutbox(
        project_id="core", entity_type="item", entity_id="GRPH-1", version=1, hash="h",
        dirty_at=T0, first_dirty_at=T0, parked_reason="contract", change_class="edit",
    ))
    db.commit()
    body = client.get("/api/sync/projects", headers=auth).json()
    assert body["status"] == "ok"
    assert "projects" in body
    row = _local(body)
    assert row["sync_level"] == "sync"
    assert row["cloud_permission"] == "comments"
    assert row["last_push_result"] == "unknown"
    assert row["queued"] == 0
    assert row["parked"] == {"count": 1, "reasons": ["contract"]}
    assert row["held_by"] == "contract"
    assert row["backfill"]["state"] in ("running", "done", "parked", "refused")
    web = _local(body, "web")
    assert web["sync_level"] == "local" and web["last_push_result"] is None
    assert web["held_by"] is None


def test_a_non_admin_is_refused_on_every_write(client, auth, db):
    ops = _login(client, "ops@ascme-labs.com")
    flush.reset_runtime()
    try:
        # Invalid level is 422 for an admin and 403 for anyone else. The 403 is
        # the route's check: the service would answer 422 once the check is gone.
        assert client.put(
            "/api/sync/projects/core", headers=ops, json={"sync_level": "nope"},
        ).status_code == 403
        assert client.put(
            "/api/sync/projects/core", headers=auth, json={"sync_level": "nope"},
        ).status_code == 422
        assert client.put(
            "/api/sync/projects/core", headers=ops, json={"sync_level": "local"},
        ).status_code == 403
        assert client.post("/api/sync/projects/core/retry-parked", headers=ops).status_code == 403
        before = client.get("/api/sync/budget", headers=ops).json()
        assert before == {
            "bytes_per_min": settings.sync_flush_bytes_per_min,
            "requests_per_min": settings.sync_flush_requests_per_min,
        }
        refused = client.put(
            "/api/sync/budget", headers=ops, json={"bytes_per_min": 10, "requests_per_min": 1},
        )
        assert refused.status_code == 403
        assert client.get("/api/sync/budget", headers=auth).json() == before
        saved = client.put(
            "/api/sync/projects/core", headers=auth, json={"sync_level": "local"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["project"]["sync_level"] == "local"
        missing = client.put(
            "/api/sync/projects/no-such", headers=auth, json={"sync_level": "local"},
        )
        assert missing.status_code == 404
    finally:
        flush.reset_runtime()


def test_permission_retry_and_budget_round_trip(client, auth, db):
    flush.reset_runtime()
    try:
        db.add(SyncProject(
            local_project_id="core", cloud_project_id="cloud-1", sync_level="sync",
            cloud_permission="comments",
        ))
        db.commit()
        item = items_svc.create_item(db, project_id="core", title="parked", status="next")
        captured = outbox.capture(
            db, project_id="core", entity_type="item", entity_id=item.key,
            change_class=outbox.CHANGE_STATUS, now=T0,
        )
        assert captured is not None, db.get(SyncProject, "core")
        captured.parked_reason = "too_large"
        db.commit()

        perm = client.put(
            "/api/sync/projects/core", headers=auth, json={"cloud_permission": "proposals"},
        )
        assert perm.status_code == 200, perm.text
        assert perm.json()["project"]["cloud_permission"] == "proposals"
        bad = client.put(
            "/api/sync/projects/core", headers=auth, json={"cloud_permission": "admin"},
        )
        assert bad.status_code == 422

        retried = client.post("/api/sync/projects/core/retry-parked", headers=auth)
        assert retried.status_code == 200, retried.text
        assert retried.json()["retried"] == 1
        with SessionLocal() as check:
            parked = check.get(SyncOutbox, ("core", "item", item.key))
            assert parked is not None and not (parked.parked_reason or "")

        negative = client.put(
            "/api/sync/budget", headers=auth, json={"bytes_per_min": -1, "requests_per_min": 1},
        )
        assert negative.status_code == 422
        updated = client.put(
            "/api/sync/budget", headers=auth, json={"bytes_per_min": 1000, "requests_per_min": 2},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {"bytes_per_min": 1000, "requests_per_min": 2}
        assert client.get("/api/sync/budget", headers=auth).json()["requests_per_min"] == 2
    finally:
        flush.reset_runtime()


def test_create_in_org_and_tag_taken(client, auth, db, monkeypatch):
    fake = FakeConsole()
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    fake.add_deployment(BEA, engineer="eng-bea", label="Bea")
    _bind_transport(monkeypatch, fake)
    _link(db)
    owned = fake.handle(httpx.Request(
        "POST", f"{CONSOLE}/api/sync/projects",
        headers={
            "authorization": f"Bearer {BEA}",
            "x-sync-contract": CONTRACT,
            "content-type": "application/json",
        },
        json={"tag": "CP", "name": "Bea's copy"},
    ))
    assert owned.status_code == 201, owned.text
    taken = client.put(
        "/api/sync/projects/core", headers=auth,
        json={"sync_level": "sync", "create_in_org": True},
    )
    assert taken.status_code == 409, taken.text
    assert taken.json()["detail"] == "tag_taken"
    assert taken.json()["owner_label"] == "Bea"
    with SessionLocal() as check:
        assert check.get(SyncProject, "core") is None

    # A free tag registers and comes back as sync. Use web (tag WA), still free.
    created = client.put(
        "/api/sync/projects/web", headers=auth,
        json={"sync_level": "sync", "create_in_org": True},
    )
    assert created.status_code == 200, created.text
    assert created.json()["project"]["sync_level"] == "sync"
    assert created.json()["project"]["project_id"] == "web"
    with SessionLocal() as check:
        mapping = check.get(SyncProject, "web")
        assert mapping is not None and mapping.cloud_project_id
        assert mapping.sync_level == "sync"


def test_a_429_reports_held_by_retry_after_and_unknown(client, auth, db, monkeypatch):
    flush.reset_runtime()
    fake = FakeConsole()
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    _bind_transport(monkeypatch, fake)
    _stub_console(monkeypatch)
    _link(db)
    created = client_mod.register_project(db, tag="GRPH", name="Graphban", transport=fake.transport())
    assert created.status == "ok" and created.cloud_project_id
    db.add(SyncProject(
        local_project_id="core", cloud_project_id=created.cloud_project_id, sync_level="sync",
    ))
    item = items_svc.create_item(db, project_id="core", title="held", status="next")
    outbox.capture(
        db, project_id="core", entity_type="item", entity_id=item.key,
        change_class=outbox.CHANGE_STATUS, now=T0,
    )
    db.commit()
    try:
        fake.retry_after_once(30)
        assert flush.flush_once(db, now=FAR) == 0
        assert flush._PROCESS.not_before is not None
        assert flush._PROCESS.not_before > datetime.now(timezone.utc)
        body = client.get("/api/sync/projects", headers=auth).json()
        row = _local(body)
        assert row["held_by"] == "retry_after"
        assert row["last_push_result"] == "unknown"
        assert row["queued"] >= 1
        assert "retry_after" not in (row["parked"]["reasons"])
    finally:
        flush.reset_runtime()
