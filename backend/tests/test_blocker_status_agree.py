"""Status and blocker cannot disagree (GRPH-1961).

A planner moved two blocked items to `backlog`; their blocker text stayed and kept them
unready with no `blocked` status to show it, so a drain idled for hours. Leaving `blocked`
now clears the blocker or is refused, and a blocker written onto a backlog/next item
makes it `blocked`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.services import items as items_svc
from tests import attest


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _blocked_on(db, text):
    it = items_svc.create_item(db, project_id="core", title="waits", status="blocked")
    items_svc.update_item(db, it.id, blocker=text)
    db.refresh(it)
    assert it.status == "blocked" and it.blocker == text
    return it


def _notes(it):
    return [e.get("detail") or "" for e in (it.evidence or []) if e.get("kind") == "note"]


# --- leaving blocked -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["backlog", "next"])
def test_leaving_blocked_is_refused_while_the_blocker_names_an_open_item(db, target):
    dep = items_svc.create_item(db, project_id="core", title="D1 parsers")
    it = _blocked_on(db, f"needs {dep.key} (D1 parsers)")
    with pytest.raises(items_svc.BlockerDisagrees) as e:
        items_svc.update_item(db, it.id, status=target)
    assert dep.key in str(e.value) and "needs" in str(e.value)
    db.refresh(it)
    assert it.status == "blocked" and it.blocker


@pytest.mark.parametrize("target", ["backlog", "next"])
def test_leaving_blocked_clears_a_prose_blocker_in_the_same_call_with_a_note(db, target):
    it = _blocked_on(db, "Needs the import command, not in this branch")
    out = items_svc.update_item(db, it.id, status=target)
    assert out.status == target
    assert out.blocker == ""
    assert ("blocker cleared on leaving blocked: Needs the import command, not in this "
            "branch") in _notes(out)


def test_leaving_blocked_clears_a_blocker_whose_named_items_are_done(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    attest.complete(db, dep.id)
    db.refresh(it)
    # GRPH-1530 already released it; the call that leaves blocked has nothing to refuse.
    assert it.blocker == ""
    assert items_svc.update_item(db, it.id, status="backlog").status == "backlog"


def test_leaving_blocked_is_refused_on_a_typed_wait(db):
    it = items_svc.create_item(db, project_id="core", title="wait", status="blocked",
                               tags=["wait:access"])
    items_svc.update_item(db, it.id, blocker="a person signs this off")
    with pytest.raises(items_svc.BlockerDisagrees) as e:
        items_svc.update_item(db, it.id, status="next")
    assert "wait:access" in str(e.value)


def test_clearing_the_blocker_in_the_same_call_is_not_refused(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    out = items_svc.update_item(db, it.id, status="next", blocker="")
    assert out.status == "next" and out.blocker == ""


# --- writing a blocker ---------------------------------------------------------------

@pytest.mark.parametrize("start", ["backlog", "next"])
def test_a_blocker_written_onto_a_backlog_or_next_item_sets_blocked(db, start):
    it = items_svc.create_item(db, project_id="core", title="t", status=start)
    out = items_svc.update_item(db, it.id, blocker="Needs the import command")
    assert out.status == "blocked" and out.blocker == "Needs the import command"


def test_a_blocker_with_an_explicit_backlog_status_is_refused(db):
    it = items_svc.create_item(db, project_id="core", title="t", status="next")
    with pytest.raises(items_svc.BlockerDisagrees):
        items_svc.update_item(db, it.id, status="backlog", blocker="Needs a thing")
    db.refresh(it)
    assert it.status == "next" and not it.blocker


def test_a_blocker_naming_only_done_items_clears_and_keeps_the_status(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    attest.complete(db, dep.id)
    it = items_svc.create_item(db, project_id="core", title="t", status="backlog")
    out = items_svc.update_item(db, it.id, blocker=f"needs {dep.key}")
    assert out.status == "backlog" and out.blocker == ""


def test_no_write_leaves_a_backlog_or_next_item_with_a_live_blocker(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    calls = [
        ("backlog", {"blocker": "prose"}),
        ("next", {"blocker": f"needs {dep.key}"}),
        ("blocked", {"blocker": f"needs {dep.key}", "status": "backlog"}),
        ("blocked", {"blocker": "prose", "status": "next"}),
    ]
    for start, fields in calls:
        it = items_svc.create_item(db, project_id="core", title="t", status=start)
        if start == "blocked":
            items_svc.update_item(db, it.id, blocker="first")
        try:
            items_svc.update_item(db, it.id, **fields)
        except items_svc.BlockerDisagrees:
            pass
        db.refresh(it)
        assert not (it.status in ("backlog", "next") and (it.blocker or "").strip()), (
            start, fields, it.status, it.blocker)


# --- the surfaces --------------------------------------------------------------------

def test_patch_refuses_leaving_blocked_with_409_naming_the_blocker(client, auth, db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    r = client.patch(f"/api/items/{it.id}", json={"status": "backlog"}, headers=auth)
    assert r.status_code == 409, r.text
    assert dep.key in r.json()["detail"]


def test_mcp_update_item_clears_a_prose_blocker_on_leaving_blocked(client, auth, db):
    it = _blocked_on(db, "Needs the import command")
    made = client.post("/api/api-keys", json={"name": "blk-writer", "scopes": ["write"],
                                              "project_id": "core"}, headers=auth).json()
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "update_item", "arguments": {"id": it.id, "status": "next"}}},
        headers={"X-API-Key": made["plaintext"]}).json()["result"]
    assert not r.get("isError"), r
    out = json.loads(r["content"][0]["text"])
    assert out["status"] == "next"
    db.refresh(it)
    assert it.blocker == ""


# --- the backfill --------------------------------------------------------------------

def _migration():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "0233_blocker_status_agree.py")
    spec = importlib.util.spec_from_file_location("migration_0233", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_moves_backlog_and_next_rows_with_a_blocker_to_blocked(db):
    legacy = []
    for status, blocker in [("backlog", "needs X"), ("next", "prose"), ("backlog", "   "),
                            ("next", ""), ("in_progress", "held")]:
        it = items_svc.create_item(db, project_id="core", title="legacy")
        it.status, it.blocker = status, blocker  # written the way rows before GRPH-1961 were
        legacy.append(it)
    db.commit()
    mod = _migration()
    with db.get_bind().begin() as conn:
        assert mod.backfill(conn) == 2
    with db.get_bind().begin() as conn:
        assert mod.backfill(conn) == 0
    for it in legacy:
        db.refresh(it)
    assert [it.status for it in legacy] == [
        "blocked", "blocked", "backlog", "next", "in_progress"]


def test_mcp_update_item_refuses_leaving_blocked_as_conflict(client, auth, db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    made = client.post("/api/api-keys", json={"name": "blk-refused", "scopes": ["write"],
                                              "project_id": "core"}, headers=auth).json()
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "update_item", "arguments": {"id": it.id, "status": "backlog"}}},
        headers={"X-API-Key": made["plaintext"]}).json()["result"]
    assert r.get("isError"), r
    err = r["structuredContent"]["error"]
    assert err["code"] == "conflict" and dep.key in err["message"]
