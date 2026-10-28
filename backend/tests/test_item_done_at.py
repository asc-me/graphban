"""`done_at`: when an item last reached done (GRPH-1134 / PRD-60 D2).

The board's done-window and auto-hide read this. `updated_at` cannot stand in for it, because
editing a finished item bumps it. Done is reached through the owner-close path here because
it is the attested route that needs no agent fleet. `_stamp_done_at` is an attribute event,
so sign_off goes through the same code.
"""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Item
from app.services import items as items_svc

MERGE = "abcdef1234567890aaaabbbbccccddddeeeeffff"
MERGE2 = "0123456789abcdef0123456789abcdef01234567"
CLOSER = {"id": "u1", "handle": "ascme", "name": "Alex Cain"}
_BACKEND = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _close(db, item_id, commit=MERGE):
    out = items_svc.owner_close(db, item_id, closer=CLOSER, commit=commit)
    db.commit()
    db.refresh(out)
    return out


def test_reaching_done_stamps_it_and_leaving_clears_it(db):
    it = items_svc.create_item(db, project_id="core", title="ship it", status="review")
    assert it.done_at is None

    done = _close(db, it.id)
    first = done.done_at
    assert first is not None
    assert items_svc.item_dict(done)["done_at"] == int(first.timestamp())

    back = items_svc.update_item(db, it.id, status="review")
    assert back.done_at is None
    # Absent, not null, on the agent read: the same rule `branch` follows.
    assert "done_at" not in items_svc.item_dict(back)

    again = _close(db, it.id, commit=MERGE2)
    assert again.done_at is not None
    assert _aware(again.done_at) >= _aware(first)


def test_editing_a_done_item_keeps_its_done_at(db):
    it = items_svc.create_item(db, project_id="core", title="before", status="review")
    stamped = _close(db, it.id).done_at
    # Push the stamp into the past so a re-stamp would show as a different value.
    old = _aware(stamped) - timedelta(days=20)
    row = db.get(Item, it.id)
    row.done_at = old
    db.commit()

    out = items_svc.update_item(db, it.id, title="after")
    assert out.title == "after"
    assert _aware(out.done_at) == old
    # Re-sending the same status is not a new arrival either.
    out = items_svc.update_item(db, it.id, status="done")
    assert _aware(out.done_at) == old


def test_done_at_rides_every_item_dict_read(client, db):
    """The ratchet, for a DONE item: the details read and the REST read both carry it."""
    it = items_svc.create_item(db, project_id="core", title="ratchet", status="review")
    done = _close(db, it.id)
    shared = items_svc.item_dict(done)
    assert "done_at" in shared
    details = items_svc.get_item_details(db, done.id)
    assert details["done_at"] == shared["done_at"]


def test_rest_item_read_carries_done_at(client, auth, db):
    it = items_svc.create_item(db, project_id="core", title="rest", status="review")
    _close(db, it.id)
    body = client.get(f"/api/items/{it.key}", headers=auth)
    assert body.status_code == 200, body.text
    assert body.json()["done_at"] is not None

    open_one = items_svc.create_item(db, project_id="core", title="open")
    body = client.get(f"/api/items/{open_one.key}", headers=auth)
    assert body.status_code == 200, body.text
    assert body.json()["done_at"] is None


# ---- migration 0145 ----------------------------------------------------------------

def _migration():
    path = _BACKEND / "alembic" / "versions" / "0145_item_done_at.py"
    spec = importlib.util.spec_from_file_location("migration_0145", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_db():
    """The two tables the backfill reads, as they stood before 0145."""
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE items (id VARCHAR PRIMARY KEY, project_id VARCHAR, number INTEGER, "
            "status VARCHAR, updated_at DATETIME)"))
        conn.execute(text(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, ts DATETIME, project_id VARCHAR, "
            "target_id VARCHAR, meta JSON)"))
    return engine


def _run(engine, fn):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def test_migration_backfills_from_events_then_updated_at_and_is_idempotent():
    from sqlalchemy import inspect, text

    engine = _legacy_db()
    signed = datetime(2026, 9, 1, 12, 0, 0)
    reopened_then_done = datetime(2026, 9, 5, 12, 0, 0)
    edited = datetime(2026, 10, 1, 12, 0, 0)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO items VALUES "
            "('ab-1','p',1,'done',:e), ('ab-2','p',2,'done',:e), ('ab-3','p',3,'next',:e)"),
            {"e": edited})
        conn.execute(text(
            "INSERT INTO events (ts, project_id, target_id, meta) VALUES "
            # ab-1, under a tag since renamed: matched by project + number; the newest wins.
            "(:s, 'p', 'OLD-1', '{\"status\": \"done\"}'), "
            "(:r, 'p', 'NEW-1', '{\"status\": \"done\"}'), "
            # Not a done event, a PRD key, and another project's 1: none count.
            "(:e, 'p', 'NEW-1', '{\"status\": \"review\"}'), "
            "(:e, 'p', 'NEW-P1', '{\"status\": \"done\"}'), "
            "(:e, 'q', 'NEW-1', '{\"status\": \"done\"}')"),
            {"s": signed, "r": reopened_then_done, "e": edited})

    mod = _migration()
    _run(engine, mod.upgrade)

    def snapshot():
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT id, done_at FROM items")).all())

    got = snapshot()
    assert str(got["ab-1"]).startswith("2026-09-05 12:00:00")  # newest done event
    assert str(got["ab-2"]).startswith("2026-10-01 12:00:00")  # no event: updated_at
    assert got["ab-3"] is None                                  # not done: stays NULL

    with engine.begin() as conn:
        mod.backfill(conn)
    assert snapshot() == got

    _run(engine, mod.downgrade)
    assert "done_at" not in {c["name"] for c in inspect(engine).get_columns("items")}
