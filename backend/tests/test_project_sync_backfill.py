"""GRPH-1736: seed a project's entities once, and do not seed them again.

The flusher sends the outbox. This file drops the session after one confirmed
batch and counts what the fake receives on the restart. Deleting the manifest
consult in ``_seed`` fails
``test_a_restart_sends_no_entity_the_manifest_already_confirmed``.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import RequestComment, SyncLink, SyncManifest, SyncOutbox, SyncProject, User
from app.security import secrets
from app.services import items as items_svc
from app.services import prds as prds_svc
from app.services import project_sync_backfill as backfill
from app.services import project_sync_client
from app.services import project_sync_flush as flush
from app.services import project_sync_outbox as outbox
from app.services import projects as projects_svc
from app.services import requests as requests_svc
from app.services.project_sync_client import CONTRACT
from tests.project_sync_fake import FakeConsole

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
DUE = T0 + timedelta(seconds=60)
WINDOW = timedelta(seconds=60)
CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_BACKFILL_TOKEN_MUST_NOT_LEAK_7f3a"
BEA = "gb_dep_PROJECT_SYNC_BACKFILL_BEA_TOKEN_MUST_NOT_LEAK_7f3a"


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class Recording(httpx.BaseTransport):
    """One transport for register and every flush. ``close`` does nothing, so
    ``httpx.Client`` cannot drop it between the contract read and the push.
    """

    def __init__(self, inner):
        self.inner = inner
        self.sent: list[tuple[str, str]] = []
        self.on_push = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sync/project-entities":
            if self.on_push is not None:
                return self.on_push(request)
            body = json.loads(gzip.decompress(request.content))
            self.sent.extend((row["type"], row["id"]) for row in body["entities"])
        return self.inner.handle_request(request)

    def close(self) -> None:
        return None


class Advertised(FakeConsole):
    """GET /api/sync/contract speaks a major the client does not."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync/contract":
            self.requests.append({
                "method": request.method, "path": request.url.path,
            })
            return self._respond(422, json={
                "detail": "contract", "field": "version", "version": "9",
            })
        return super().handle(request)


class RefuseOne(FakeConsole):
    def __init__(self, ident: str):
        super().__init__()
        self.ident = ident

    def _one_entity(self, replica, entity):
        if isinstance(entity, dict) and entity.get("id") == self.ident:
            version = entity.get("version")
            return {
                "type": entity.get("type") if isinstance(entity.get("type"), str) else "",
                "id": self.ident,
                "version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
            }, "forbidden_type"
        return super()._one_entity(replica, entity)


def _user(db) -> None:
    db.add(User(
        id="user_backfill", name="Ada", handle="ada", email="ada@example.test", password_hash="x",
    ))
    db.commit()


def _project(db):
    _user(db)
    return projects_svc.create_project(
        db, name="Backfill", owner_user_id="user_backfill", tag="BACK",
    )


def _link(db, fake: FakeConsole) -> Recording:
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    db.add(SyncLink(
        id="instance", console_url=CONSOLE, cloud_url=CONSOLE, token_enc=secrets.encrypt(TOKEN),
    ))
    db.commit()
    return Recording(fake.transport())


def _item(db, project_id: str, title: str):
    return items_svc.create_item(db, project_id=project_id, title=title, status="next")


def _counts(wire: Recording) -> Counter:
    return Counter(wire.sent)


def _queued(db, project_id: str) -> set[tuple[str, str]]:
    rows = db.scalars(select(SyncOutbox).where(SyncOutbox.project_id == project_id)).all()
    return {(row.entity_type, row.entity_id) for row in rows}


def test_a_missing_project_is_refused_and_an_empty_one_is_done(db):
    assert backfill.backfill_status(db, "no-such-project") == {
        "total": 0, "confirmed": 0, "parked": 0, "state": "refused",
    }
    project = _project(db)
    fake = FakeConsole()
    wire = _link(db, fake)
    started = backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert started["state"] == "done"
    assert started["confirmed"] == started["total"] == 0
    assert started["seeded"] == 0
    mapping = db.get(SyncProject, project.id)
    assert mapping is not None and mapping.cloud_project_id
    assert mapping.sync_level == outbox.SYNC_LEVEL_SYNC


def test_create_in_org_tag_taken_stores_no_mapping_and_returns_the_owner(db):
    project = _project(db)
    fake = FakeConsole()
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    fake.add_deployment(BEA, engineer="eng-bea", label="Bea")
    owned = fake.handle(httpx.Request(
        "POST", f"{CONSOLE}/api/sync/projects",
        headers={
            "authorization": f"Bearer {BEA}",
            "x-sync-contract": CONTRACT,
            "content-type": "application/json",
        },
        json={"tag": project.tag, "name": "Bea's copy"},
    ))
    assert owned.status_code == 201
    db.add(SyncLink(
        id="instance", console_url=CONSOLE, cloud_url=CONSOLE, token_enc=secrets.encrypt(TOKEN),
    ))
    db.commit()
    _item(db, project.id, "must not be seeded")

    started = backfill.start_backfill(
        db, project.id, True, transport=fake.transport(), now=T0,
    )
    assert started["state"] == "refused"
    assert started["reason"] == "tag_taken"
    assert started["owner_label"] == "Bea"
    assert started["seeded"] == 0
    assert db.get(SyncProject, project.id) is None
    assert _queued(db, project.id) == set()
    assert project.tag == "BACK"


def test_backfill_reaches_done_when_the_fake_has_confirmed_every_entity(db):
    project = _project(db)
    item = _item(db, project.id, "the work")
    wait = items_svc.create_item(
        db, project_id=project.id, title="Need a decision", status="blocked",
        tags=["wait:decision"], question="Ship it?", options=["yes", "no"],
    )
    prd = prds_svc.create_prd(
        db, title="Sync", project_id=project.id, created_by="user_backfill",
        body="## What syncs\n\nEverything, once.\n",
    )
    req = requests_svc.create_request(
        db, type_="bug", title="a bug", project_id=project.id, by="ada",
    )
    db.add(RequestComment(
        id="rcom_backfill", request_id=req.id, author_user_id="user_backfill",
        author_kind="operator", body="looks wrong",
    ))
    db.commit()

    fake = FakeConsole()
    wire = _link(db, fake)
    started = backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert started["seeded"] == started["total"]
    assert started["total"] >= 5  # two items, the wait, the prd, the comment
    assert started["state"] == "running"
    assert started["confirmed"] == 0
    mapping = db.get(SyncProject, project.id)
    assert mapping is not None and mapping.cloud_project_id
    assert mapping.cloud_project_tag == project.tag

    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) >= 1
    status = backfill.backfill_status(db, project.id)
    assert status["state"] == "done"
    assert status["confirmed"] == status["total"]
    assert status["parked"] == 0

    stored = {(row["type"], row["id"]) for row in fake.entities(mapping.cloud_project_id)}
    assert ("item", item.key) in stored
    assert ("item", wait.key) in stored
    assert ("wait", wait.key) in stored
    assert ("prd", prd.key) in stored
    assert ("comment", "rcom_backfill") in stored
    assert _counts(wire)[("item", item.key)] == 1


def test_a_restart_sends_no_entity_the_manifest_already_confirmed(db, monkeypatch):
    """One confirmed batch, then the session is dropped. The restart must not
    send that entity again. The send count is on the fake. The outbox check is
    what makes reseeding fail this test even though the flusher would also
    skip a matching manifest hash.
    """
    monkeypatch.setattr(flush, "BATCH_ENTITIES", 1)
    monkeypatch.setattr(settings, "sync_flush_requests_per_min", 1)
    project = _project(db)
    items = [_item(db, project.id, f"row-{i}") for i in range(3)]
    # Keys before the session is dropped. The Item's key loads its project.
    item_keys = [item.key for item in items]
    fake = FakeConsole()
    wire = _link(db, fake)
    created = project_sync_client.register_project(
        db, tag=project.tag, name=project.name, transport=wire,
    )
    assert created.status == "ok" and created.cloud_project_id
    db.add(SyncProject(
        local_project_id=project.id, cloud_project_id=created.cloud_project_id,
        cloud_project_tag=project.tag, sync_level=outbox.SYNC_LEVEL_SYNC,
    ))
    db.commit()

    started = backfill.start_backfill(db, project.id, False, now=T0)
    assert started["seeded"] == 3
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 1
    confirmed = {
        (row.entity_type, row.entity_id)
        for row in db.scalars(select(SyncManifest).where(SyncManifest.project_id == project.id))
    }
    assert len(confirmed) == 1
    project_id = project.id
    # The process died. What flush committed (the manifest, the deleted outbox
    # row) is all the next process can see.
    db.close()

    restarted = SessionLocal()
    try:
        again = backfill.start_backfill(restarted, project_id, False, now=DUE)
        assert again["skipped"] == 1
        queued = _queued(restarted, project_id)
        assert confirmed.isdisjoint(queued)
        assert flush.flush_once(
            restarted, now=DUE + WINDOW, transport=wire, state=flush.FlushState(), rng=lambda: 0,
        ) == 1
        counts = _counts(wire)
        for ident in confirmed:
            assert counts[ident] == 1
        # The other two were seeded and one of them went out on the restart.
        assert sum(counts[("item", key)] for key in item_keys) == 2
    finally:
        restarted.close()


def test_a_restart_edit_sends_a_version_above_the_last_confirmed_one(db):
    """Backfill after a restart (manifest kept, outbox empty) must not reuse
    the confirmed version. The console would acknowledge a lower one and keep
    the old body.
    """
    project = _project(db)
    item = _item(db, project.id, "stable")
    fake = FakeConsole()
    wire = _link(db, fake)
    backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 1
    manifest = db.get(SyncManifest, (project.id, "item", item.key))
    assert manifest is not None
    confirmed = manifest.version
    assert db.get(SyncOutbox, (project.id, "item", item.key)) is None
    item.title = "edited after restart"
    db.commit()
    again = backfill.start_backfill(db, project.id, False, now=DUE)
    assert again["seeded"] == 1
    assert flush.flush_once(
        db, now=DUE + WINDOW, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 1
    resent = db.get(SyncManifest, (project.id, "item", item.key))
    assert resent.version > confirmed
    assert 0 <= resent.version <= outbox.MAX_SAFE_VERSION
    stored = [row for row in fake.entities(manifest_cloud(db, project.id)) if row["id"] == item.key]
    assert len(stored) == 1
    assert stored[0]["body"]["title"] == "edited after restart"
    assert stored[0]["version"] == resent.version


def manifest_cloud(db, project_id: str) -> str:
    mapping = db.get(SyncProject, project_id)
    assert mapping is not None and mapping.cloud_project_id
    return mapping.cloud_project_id


def test_a_confirmed_entity_whose_body_changed_is_seeded_again(db):
    project = _project(db)
    item = _item(db, project.id, "stable")
    fake = FakeConsole()
    wire = _link(db, fake)
    backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 1
    assert backfill.backfill_status(db, project.id)["state"] == "done"
    item.title = "edited after confirm"
    db.commit()
    again = backfill.start_backfill(db, project.id, False, now=DUE)
    assert again["seeded"] == 1
    assert again["skipped"] == 0
    assert ("item", item.key) in _queued(db, project.id)


def test_a_timeout_is_not_confirmed(db):
    project = _project(db)
    item = _item(db, project.id, "blip")
    fake = FakeConsole()
    wire = _link(db, fake)

    def fail(request):
        del request
        raise httpx.ReadTimeout("timed out")

    # The contract read has to succeed. Only the entity push times out, which
    # is the outcome the status must not count as confirmed.
    wire.on_push = fail
    backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 0
    status = backfill.backfill_status(db, project.id)
    assert status["confirmed"] == 0
    assert status["state"] == "running"
    assert status["total"] >= 1
    assert db.get(SyncManifest, (project.id, "item", item.key)) is None


def test_an_entity_refusal_parks_and_the_status_is_parked(db):
    project = _project(db)
    kept = _item(db, project.id, "kept")
    blocked = _item(db, project.id, "blocked")
    fake = RefuseOne(blocked.key)
    wire = _link(db, fake)
    backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) >= 1
    status = backfill.backfill_status(db, project.id)
    assert status["state"] == "parked"
    assert status["confirmed"] == 1
    assert status["parked"] == 1
    assert status["confirmed"] != status["total"]
    assert db.get(SyncManifest, (project.id, "item", kept.key)) is not None
    assert db.get(SyncManifest, (project.id, "item", blocked.key)) is None


def test_a_contract_refusal_is_refused_and_confirms_nothing(db):
    project = _project(db)
    _item(db, project.id, "contract")
    fake = Advertised()
    wire = _link(db, fake)
    backfill.start_backfill(db, project.id, True, transport=wire, now=T0)
    assert flush.flush_once(
        db, now=DUE, transport=wire, state=flush.FlushState(), rng=lambda: 0,
    ) == 0
    status = backfill.backfill_status(db, project.id)
    assert status["state"] == "refused"
    assert status["confirmed"] == 0
    assert status["parked"] == status["total"]
    assert status["total"] >= 1
