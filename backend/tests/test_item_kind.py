"""GRPH-1358. Items carry a `kind` (feature / fix / chore); empty = not recorded."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models import Item, Project
from app.services import items as items_svc
from app.services import prds as prd_svc
from tests.prd_approve import approve


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    s.add(Project(id="kp", name="Kind", tag="KP"))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "kind"},
                       headers=auth).json()["plaintext"]


def test_mcp_create_reads_back_and_refuses_bad_value(client, key):
    made = _rpc(client, key, "create_item", {"title": "t", "kind": "feature"})
    assert not made.get("isError"), made
    iid = made["structuredContent"]["id"]
    got = _rpc(client, key, "get_item_details", {"id": iid})["structuredContent"]
    assert got["kind"] == "feature"

    bad = _rpc(client, key, "create_item", {"title": "t", "kind": "feat"})
    assert bad.get("isError") is True
    msg = str(bad["structuredContent"]["error"])
    assert "feature" in msg and "fix" in msg and "chore" in msg

    bad_upd = _rpc(client, key, "update_item", {"id": iid, "kind": "feat"})
    assert bad_upd.get("isError") is True
    ok = _rpc(client, key, "update_item", {"id": iid, "kind": "chore"})
    assert ok["structuredContent"]["kind"] == "chore"


def test_rest_list_and_detail_carry_kind(client, auth):
    r = client.post("/api/items", json={"title": "t", "kind": "fix"}, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "fix"
    bad = client.post("/api/items", json={"title": "t", "kind": "feat"}, headers=auth)
    assert bad.status_code == 422
    rows = client.get("/api/items", params={"project_id": r.json()["project_id"]}, headers=auth).json()
    assert any(isinstance(row, dict) and row.get("kind") == "fix" for row in rows), rows


def test_decompose_create_tasks_are_features_and_bug_tag_is_fix(db):
    prd = prd_svc.create_prd(db, title="Kinds", project_id="kp")
    approve(db, prd)
    out = prd_svc.decompose(db, prd, create=True)
    assert out["created"], out
    for iid in out["created"]:
        assert items_svc.item_dict(db.get(Item, iid))["kind"] == "feature"

    bug = items_svc.create_item(db, title="b", tags=["bug"], project_id="kp")
    assert items_svc.item_dict(bug)["kind"] == "fix"
    plain = items_svc.create_item(db, title="p", project_id="kp")
    assert items_svc.item_dict(plain)["kind"] == ""
    explicit = items_svc.create_item(db, title="e", tags=["bug"], kind="chore", project_id="kp")
    assert explicit.kind == "chore"


def test_a_pre_migration_row_reads_empty_not_feature(db):
    item = items_svc.create_item(db, title="old", project_id="kp")
    # A row written before the column existed carries the server default.
    db.execute(text("UPDATE items SET kind = '' WHERE id = :i"), {"i": item.id})
    db.commit()
    db.refresh(item)
    assert items_svc.item_dict(item)["kind"] == ""
    assert items_svc.item_dict(item)["kind"] != "feature"
