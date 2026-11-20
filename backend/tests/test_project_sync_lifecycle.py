"""GRPH-1738: unsync, purge, and unlink for a synced project.

Clearing the manifest before DELETE is confirmed fails
``test_a_timeout_keeps_the_manifest_until_the_purge_is_confirmed``.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, select, text

from app.db import SessionLocal
from app.errors import Refused
from app.models import Item, SyncLink, SyncManifest, SyncOutbox, SyncProject, User
from app.security import secrets
from app.services import items as items_svc
from app.services import link_device
from app.services import project_sync_client as client
from app.services import project_sync_flush as flush
from app.services import project_sync_lifecycle as life
from app.services import project_sync_outbox as outbox
from app.services import projects as projects_svc
from tests.project_sync_fake import FakeConsole

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
DUE = T0 + timedelta(seconds=60)
CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_LIFECYCLE_TOKEN_MUST_NOT_LEAK_7f3a"
ENTITY = {
    "type": "item",
    "id": "GRPH-1",
    "version": 1,
    "hash": "sha256:" + "ab" * 32,
    "body": {"title": "one"},
}


@pytest.fixture()
def db(client):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _user(db, email: str) -> User:
    return db.scalars(select(User).where(User.email == email)).one()


def _utc(value: datetime) -> datetime:
    """SQLite hands timezone-aware columns back naive. Both mean UTC here."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _link(db, fake: FakeConsole) -> str:
    fake.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    db.add(SyncLink(id="instance", console_url=CONSOLE, cloud_url=CONSOLE,
                    token_enc=secrets.encrypt(TOKEN)))
    db.commit()
    created = client.register_project(
        db, tag="P89L", name="Lifecycle", transport=fake.transport())
    assert created.status == "ok" and created.cloud_project_id
    return created.cloud_project_id


def _mapping(db, project_id: str, cloud_id: str, level: str = outbox.SYNC_LEVEL_SYNC) -> None:
    db.add(SyncProject(
        local_project_id=project_id, cloud_project_id=cloud_id, sync_level=level,
    ))
    db.commit()


def _manifest(db, project_id: str = "core") -> None:
    db.add(SyncManifest(
        project_id=project_id, entity_type="item", entity_id="GRPH-1",
        version=1, hash=ENTITY["hash"], confirmed_at=T0,
    ))
    db.commit()


def _outbox(db, project_id: str = "core") -> None:
    db.add(SyncOutbox(
        project_id=project_id, entity_type="item", entity_id="GRPH-1",
        version=1, hash="pending", dirty_at=T0, first_dirty_at=T0,
        attempts=0, change_class=outbox.CHANGE_EDIT,
    ))
    db.commit()


def _rows(db, model, project_id: str):
    db.expire_all()
    column = model.project_id if model is not SyncProject else model.local_project_id
    return list(db.scalars(select(model).where(column == project_id)).all())


class _Capture(httpx.BaseTransport):
    """Records entity ids on each project-entities POST. ``close`` is a no-op
    so the contract read and the push share one transport."""

    def __init__(self, inner, clock: dict):
        self.inner = inner
        self.clock = clock
        self.sent: list[list[str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (request.method == "POST"
                and request.url.path == "/api/sync/project-entities"):
            body = json.loads(gzip.decompress(request.content))
            self.sent.append([entity["id"] for entity in body["entities"]])
        return self.inner.handle_request(request)

    def close(self) -> None:
        return None


def test_unsync_with_purge_deletes_the_replica_and_records_who(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    _mapping(db, "core", cloud_id)
    _outbox(db)
    _manifest(db)
    pushed = client.push_entities(
        db, cloud_project_id=cloud_id, entities=[ENTITY], transport=fake.transport())
    assert pushed.status == "ok"
    assert fake.entities(cloud_id)

    admin = _user(db, "alex@ascme-labs.com")
    result = life.set_level(
        db, "core", outbox.SYNC_LEVEL_DIGESTS, user=admin, now=T0,
        transport=fake.transport())

    assert result["purged"] is True and result["pending_purge"] is False
    assert result["level"] == outbox.SYNC_LEVEL_DIGESTS
    # The fake's read of the project is empty once DELETE has landed. That is
    # the 404 / empty the acceptance names: the replica's entities are gone.
    assert fake.entities(cloud_id) == []
    assert _rows(db, SyncManifest, "core") == []
    assert _rows(db, SyncOutbox, "core") == []
    mapping = db.get(SyncProject, "core")
    assert mapping.sync_level == outbox.SYNC_LEVEL_DIGESTS
    assert mapping.pending_purge is None
    assert mapping.enabled_by == "alex@ascme-labs.com"
    assert _utc(mapping.enabled_at) == T0
    assert outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-1",
        change_class=outbox.CHANGE_EDIT, now=T0,
    ) is None


def test_unsync_without_purge_keeps_the_manifest(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    _mapping(db, "core", cloud_id)
    _manifest(db)
    before = len(fake.requests)
    result = life.set_level(
        db, "core", outbox.SYNC_LEVEL_DIGESTS, purge=False,
        user=_user(db, "alex@ascme-labs.com"), now=T0, transport=fake.transport())
    assert result["purged"] is False and result["pending_purge"] is False
    assert _rows(db, SyncManifest, "core") != []
    assert not any(call["method"] == "DELETE" for call in fake.requests[before:])


def test_a_timeout_keeps_the_manifest_until_the_purge_is_confirmed(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    _mapping(db, "core", cloud_id)
    _manifest(db)
    pushed = client.push_entities(
        db, cloud_project_id=cloud_id, entities=[ENTITY], transport=fake.transport())
    assert pushed.status == "ok" and fake.entities(cloud_id)
    fake.timeout_once()

    admin = _user(db, "alex@ascme-labs.com")
    result = life.set_level(
        db, "core", outbox.SYNC_LEVEL_DIGESTS, user=admin, now=T0,
        transport=fake.transport())

    assert result["purged"] is False and result["pending_purge"] is True
    assert result["level"] == outbox.SYNC_LEVEL_DIGESTS
    mapping = db.get(SyncProject, "core")
    assert mapping.pending_purge == outbox.SYNC_LEVEL_DIGESTS
    assert mapping.sync_level == outbox.SYNC_LEVEL_DIGESTS
    assert _rows(db, SyncManifest, "core") != []
    assert fake.entities(cloud_id)  # the replica is still there

    retried = life.retry_pending_purges(db, transport=fake.transport())
    assert len(retried) == 1
    assert retried[0]["purged"] is True and retried[0]["pending_purge"] is False
    assert _rows(db, SyncManifest, "core") == []
    assert db.get(SyncProject, "core").pending_purge is None
    assert fake.entities(cloud_id) == []


def test_local_only_with_a_timeout_retries_then_drops_the_row(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    _mapping(db, "core", cloud_id)
    _manifest(db)
    fake.timeout_once()
    admin = _user(db, "alex@ascme-labs.com")
    result = life.set_level(
        db, "core", life.LEVEL_LOCAL, user=admin, now=T0, transport=fake.transport())
    assert result["purged"] is False and result["pending_purge"] is True
    assert db.get(SyncProject, "core") is not None  # the marker still needs the row
    assert _rows(db, SyncManifest, "core") != []

    retried = life.retry_pending_purges(db, transport=fake.transport())
    assert retried[0]["purged"] is True and retried[0]["level"] == life.LEVEL_LOCAL
    assert db.get(SyncProject, "core") is None
    assert _rows(db, SyncManifest, "core") == []


def test_a_non_admin_cannot_change_the_level_or_the_permission(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    _mapping(db, "core", cloud_id)
    member = _user(db, "ops@ascme-labs.com")
    before = len(fake.requests)
    with pytest.raises(Refused) as refused:
        life.set_level(db, "core", outbox.SYNC_LEVEL_DIGESTS, user=member, now=T0,
                       transport=fake.transport())
    assert refused.value.status_code == 403
    assert refused.value.rule_id == "sync.link_needs_box_admin"
    assert db.get(SyncProject, "core").sync_level == outbox.SYNC_LEVEL_SYNC
    with pytest.raises(Refused) as refused_permission:
        life.set_cloud_permission(
            db, "core", outbox.CLOUD_PERMISSION_READ_ONLY, user=member, now=T0)
    assert refused_permission.value.status_code == 403
    assert not any(call["method"] == "DELETE" for call in fake.requests[before:])

    admin = _user(db, "alex@ascme-labs.com")
    changed = life.set_cloud_permission(
        db, "core", outbox.CLOUD_PERMISSION_READ_ONLY, user=admin, now=T0)
    assert changed["cloud_permission"] == outbox.CLOUD_PERMISSION_READ_ONLY
    assert changed["enabled_by"] == "alex@ascme-labs.com"
    assert _utc(db.get(SyncProject, "core").enabled_at) == T0


def test_unlink_then_relink_sends_only_entities_changed_while_unlinked(db):
    fake = FakeConsole()
    cloud_id = _link(db, fake)
    admin = _user(db, "alex@ascme-labs.com")
    project = projects_svc.create_project(db, name="Lifecycle box", owner_user_id=admin.id,
                                           tag="P89B")
    project_id = project.id
    _mapping(db, project_id, cloud_id)
    kept = items_svc.create_item(db, project_id=project_id, title="kept", status="next")
    edited = items_svc.create_item(db, project_id=project_id, title="edited", status="next")
    outbox.capture(
        db, project_id=project_id, entity_type="item", entity_id=kept.key,
        change_class=outbox.CHANGE_NEW, now=T0,
    )
    outbox.capture(
        db, project_id=project_id, entity_type="item", entity_id=edited.key,
        change_class=outbox.CHANGE_NEW, now=T0,
    )
    db.commit()
    clock = {"now": DUE}
    wire = _Capture(fake.transport(), clock)
    state = flush.FlushState()
    assert flush.flush_once(db, now=DUE, transport=wire, state=state, rng=lambda: 0) == 1
    assert {entity_id for batch in wire.sent for entity_id in batch} == {kept.key, edited.key}
    assert db.get(SyncManifest, (project_id, "item", kept.key)) is not None
    assert db.get(SyncManifest, (project_id, "item", edited.key)) is not None

    link_device.clear(db)
    assert db.get(SyncProject, project_id) is None
    assert db.get(SyncManifest, (project_id, "item", kept.key)) is not None
    assert db.get(SyncManifest, (project_id, "item", edited.key)) is not None
    pulled = client.pull_proposals(db, project=cloud_id, transport=fake.transport())
    assert pulled.status == "unknown" and pulled.reason == "not_linked"

    changed = db.get(Item, edited.id)
    changed.title = "edited while the box was unlinked"
    db.commit()

    link = db.get(SyncLink, "instance")
    link.token_enc = secrets.encrypt(TOKEN)
    link.console_url = link.cloud_url = CONSOLE
    db.add(SyncProject(
        local_project_id=project_id, cloud_project_id=cloud_id,
        sync_level=outbox.SYNC_LEVEL_DIGESTS,
    ))
    db.commit()
    life.set_level(db, project_id, outbox.SYNC_LEVEL_SYNC, user=admin, now=DUE,
                   transport=fake.transport())
    wire.sent.clear()
    later = DUE + timedelta(seconds=60)
    assert flush.flush_once(db, now=later, transport=wire, state=state, rng=lambda: 0) == 1
    sent = [entity_id for batch in wire.sent for entity_id in batch]
    assert sent == [edited.key]


def test_existing_rows_gain_a_null_pending_purge():
    path = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0216_sync_pending_purge.py"
    spec = importlib.util.spec_from_file_location("mig_0216", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sync_project (local_project_id VARCHAR PRIMARY KEY)"))
        conn.execute(text(
            "INSERT INTO sync_project (local_project_id) VALUES ('core')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
        row = conn.execute(text(
            "SELECT pending_purge FROM sync_project WHERE local_project_id = 'core'")).one()
    assert row[0] is None

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.downgrade()
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_project)")).all()}
    assert "pending_purge" not in cols
