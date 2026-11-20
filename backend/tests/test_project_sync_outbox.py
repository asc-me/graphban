"""Sync outbox: one row per changed entity, only for projects at `sync` (GRPH-1733)."""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.db import SessionLocal
from app.models import Event, SyncOutbox, SyncProject
from app.services import events
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import project_sync_outbox as outbox
from app.services import waits

T0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mapping(db, level=outbox.SYNC_LEVEL_SYNC, project_id="core"):
    db.add(SyncProject(local_project_id=project_id, cloud_project_id="cloud", sync_level=level))
    db.commit()


def _record(db, **kw):
    kw.setdefault("actor_type", "user")
    kw.setdefault("surface", "rest")
    kw.setdefault("project_id", "core")
    kw.setdefault("target_type", "item")
    kw.setdefault("target_id", "GRPH-1")
    kw.setdefault("action", "update_item")
    return events.record(db, **kw)


def _rows(db, entity_id=None):
    db.expire_all()
    q = db.query(SyncOutbox)
    if entity_id is not None:
        q = q.filter(SyncOutbox.entity_id == entity_id)
    return q.all()


def test_alembic_head_is_the_proposal_migration():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = pathlib.Path(__file__).parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0213"]
    assert script.get_revision("0213").down_revision == "0212"  # GRPH-1768, manifest bigint
    assert script.get_revision("0212").down_revision == "0211"
    assert script.get_revision("0211").down_revision == "0210"


def test_existing_sync_project_rows_read_digests_and_comments():
    """A row written before 0211 keeps today's behaviour: digests, comments, no enabler."""
    path = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0211_sync_outbox.py"
    spec = importlib.util.spec_from_file_location("mig_0211", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sync_project ("
            "local_project_id VARCHAR PRIMARY KEY, cloud_project_id VARCHAR)"))
        conn.execute(text(
            "INSERT INTO sync_project (local_project_id, cloud_project_id) "
            "VALUES ('core', 'cloud-1')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
        row = conn.execute(text(
            "SELECT sync_level, cloud_permission, enabled_by, enabled_at "
            "FROM sync_project WHERE local_project_id = 'core'")).one()
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'")).all()}
    assert row[0] == "digests"
    assert row[1] == "comments"
    assert row[2] is None and row[3] is None
    assert {"sync_outbox", "sync_manifest"} <= tables

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.downgrade()
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'")).all()}
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_project)")).all()}
    assert "sync_outbox" not in tables and "sync_manifest" not in tables
    assert "sync_level" not in cols and "cloud_permission" not in cols


def test_a_project_at_digests_or_with_no_row_writes_no_outbox_rows(db):
    _record(db, meta={"status": "in_progress"})
    assert _rows(db) == []

    _mapping(db, outbox.SYNC_LEVEL_DIGESTS)
    _record(db, meta={"status": "in_progress"})
    _record(db, action="create_prd", target_type="prd", target_id="GRPH-P1")
    assert _rows(db) == []


def test_five_hundred_status_changes_collapse_to_one_row_version_500(db):
    """Inserting a new row per change, instead of upserting, fails this count."""
    _mapping(db)
    _record(db, meta={"status": "in_progress"})
    first = _rows(db, "GRPH-1")[0].first_dirty_at
    for n in range(499):
        _record(db, meta={"status": "in_progress" if n % 2 == 0 else "next"})
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].entity_type == "item"
    assert rows[0].entity_id == "GRPH-1"
    assert rows[0].version == 500
    assert rows[0].first_dirty_at == first
    assert rows[0].dirty_at >= first
    assert rows[0].change_class == outbox.CHANGE_STATUS
    assert rows[0].attempts == 0
    assert outbox.backlog_bytes(db) < outbox.CAP_BYTES
    assert outbox.backlog_bytes(db) == outbox._row_bytes(rows[0])


def test_a_later_lesser_change_does_not_hide_a_status_change(db):
    _mapping(db)
    _record(db, action="create_item", target_id="GRPH-2")
    _record(db, action="update_item", target_id="GRPH-2", meta={"evidence": [{"kind": "note"}]})
    row = _rows(db, "GRPH-2")[0]
    assert row.version == 2
    assert row.change_class == outbox.CHANGE_NEW

    _record(db, action="update_item", target_id="GRPH-2", meta={"status": "blocked"})
    row = _rows(db, "GRPH-2")[0]
    assert row.version == 3
    assert row.change_class == outbox.CHANGE_STATUS


def test_a_refusal_and_a_heartbeat_write_nothing(db):
    _mapping(db)
    _record(db, action="refusal", meta={"status": 403})
    _record(db, action="heartbeat")
    assert _rows(db) == []


def test_a_request_comment_is_its_own_new_entity(db):
    _mapping(db)
    _record(db, action="request_comment", target_type="request", target_id="GRPH-R1",
            meta={"comment_id": "rcom_1"})
    rows = _rows(db)
    assert len(rows) == 1
    assert (rows[0].entity_type, rows[0].entity_id, rows[0].change_class) == (
        "comment", "rcom_1", outbox.CHANGE_NEW)


def test_a_decision_wait_answer_dirties_the_item_as_a_status_change(db):
    _mapping(db)
    original = items_svc.create_item(db, project_id="core", title="feature", status="in_progress")
    wait = items_svc.create_item(
        db, project_id="core", title="which store?", status="blocked",
        question="Which store?", options=["redis", "postgres"])
    links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, original.id, status="blocked")
    assert _rows(db) == []  # those writes are not audit events

    waits.answer_wait(db, wait.id, choice="redis")
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].entity_id == wait.key
    assert rows[0].change_class == outbox.CHANGE_STATUS
    assert rows[0].version == 1
    assert db.query(Event).filter(Event.action == "blocker.answer").count() == 1


def test_due_honours_debounce_max_delay_and_priority(db):
    _mapping(db)
    outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-1",
        change_class=outbox.CHANGE_STATUS, now=T0)
    db.commit()
    assert outbox.due(db, T0 + timedelta(seconds=4)) == []
    assert [r.entity_id for r in outbox.due(db, T0 + timedelta(seconds=5))] == ["GRPH-1"]

    # Still being edited at t=59s. Debounce has not elapsed; the 60s max delay has.
    outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-1",
        change_class=outbox.CHANGE_STATUS, now=T0 + timedelta(seconds=59))
    db.commit()
    assert outbox.due(db, T0 + timedelta(seconds=59)) == []
    assert [r.entity_id for r in outbox.due(db, T0 + timedelta(seconds=60))] == ["GRPH-1"]

    specs = [
        ("item", "GRPH-E", outbox.CHANGE_EDIT, T0),
        ("item", "GRPH-S", outbox.CHANGE_STATUS, T0),
        ("item", "GRPH-H", outbox.CHANGE_HISTORY, T0),
        ("prd", "GRPH-P9", outbox.CHANGE_NEW, T0),
        # Same class as GRPH-E, waiting longer, so it sorts ahead of that edit.
        ("item", "GRPH-E2", outbox.CHANGE_EDIT, T0 - timedelta(seconds=10)),
    ]
    for entity_type, entity_id, change_class, when in specs:
        outbox.capture(
            db, project_id="core", entity_type=entity_type, entity_id=entity_id,
            change_class=change_class, now=when)
    parked = outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-PARKED",
        change_class=outbox.CHANGE_STATUS, now=T0 - timedelta(minutes=5))
    parked.parked_reason = "422 contract"
    db.commit()

    assert [r.entity_id for r in outbox.due(db, T0 + timedelta(seconds=60))] == [
        "GRPH-1", "GRPH-S", "GRPH-P9", "GRPH-E2", "GRPH-E", "GRPH-H"]


def test_capture_does_not_clear_a_parked_reason(db):
    _mapping(db)
    row = outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-1",
        change_class=outbox.CHANGE_STATUS, now=T0)
    row.parked_reason = "429"
    row.parked_contract = "1"
    db.commit()
    again = outbox.capture(
        db, project_id="core", entity_type="item", entity_id="GRPH-1",
        change_class=outbox.CHANGE_EDIT, now=T0 + timedelta(seconds=10))
    db.commit()
    assert again.version == 2
    assert again.parked_reason == "429"
    assert again.parked_contract == "1"
    assert again.change_class == outbox.CHANGE_STATUS
    assert outbox.due(db, T0 + timedelta(hours=1)) == []


def test_an_empty_outbox_measures_zero_bytes(db):
    assert outbox.backlog_bytes(db) == 0


def test_the_module_does_no_network_io():
    source = pathlib.Path(outbox.__file__).read_text()
    for banned in ("httpx", "requests", "urllib", "socket"):
        assert banned not in source
