"""GRPH-821 — `get_item_details` is the full record, built ON the shared renderer.

The read that calls itself "the full record for one item — its description, blockers,
dependencies, and linked memory shards" built its own dict, and so carried no `project_id`,
no `touchpoints`, no `prd_id`/`prd_section` and no links at the top level, while every other
item read did. Fourth instance of one pattern (intent hold, bounce, branch, then this). The
fix is structural — the details read starts from `items_svc.item_dict` — and the last test
here is the ratchet: whatever the shared renderer emits, this read emits too.
"""
from __future__ import annotations

import pytest

from app.models import Item
from app.services import items as items_svc
from app.services import keys


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Full record"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_the_details_read_carries_the_shared_item_fields(client, key, proj):
    """Sabotage: drop `prd_id` from the details dict and a worker claiming a PRD-41 slice
    cannot see which section it implements — the field `update_item` tracks drift on."""
    prd = _ok(_mcp(client, key, "create_prd", {
        "title": "P", "body": "## S1 — The axis\n\nBuild the axis.\n", "project_id": proj}))
    item = _ok(_mcp(client, key, "create_item", {
        "title": "slice", "status": "next", "touchpoints": ["backend/app/services/harness.py"],
        "prd_id": prd["id"], "prd_section": "S1 — The axis"}))
    details = _ok(_mcp(client, key, "get_item_details", {"id": item["id"]}))
    assert details["project_id"] == proj
    assert details["touchpoints"] == ["backend/app/services/harness.py"]
    assert details["prd_id"] == prd["id"]
    assert details["prd_section"] == "S1 — The axis"
    # The fields this read always had are still here — the rebuild lost nothing.
    for present in ("description", "blocker", "pr", "branch", "linked_shards",
                    "linked_requests", "brief", "built_by", "reviewed_by", "evidence"):
        assert present in details, present


def test_the_details_read_lists_links_in_both_directions(client, key):
    """The tool promises "dependencies". `brief.blocked_by` is one direction of one type,
    unfinished only; `links` is every edge, endpoints rendered under the current tag."""
    a = _ok(_mcp(client, key, "create_item", {"title": "S2", "status": "backlog"}))["id"]
    b = _ok(_mcp(client, key, "create_item", {"title": "S1", "status": "next"}))["id"]
    _ok(_mcp(client, key, "link_items", {"a": a, "b": b, "type": "dependency",
                                          "reason": "S2 builds on S1"}))
    for side in (a, b):
        details = _ok(_mcp(client, key, "get_item_details", {"id": side}))
        assert details["links"] == [
            {"type": "dependency", "a": a, "b": b, "reason": "S2 builds on S1"}], details["links"]
    lone = _ok(_mcp(client, key, "create_item", {"title": "S3"}))["id"]
    # Present and empty, never absent: "no links" and "this read has no such field" differ.
    assert _ok(_mcp(client, key, "get_item_details", {"id": lone}))["links"] == []


def test_the_shared_renderer_cannot_outgrow_this_read(client, key, db):
    """The ratchet. Every key `item_dict` emits is on the details read, so the next field
    added to the shared renderer — for the backlog, for a claim reply — cannot skip the read
    an agent makes right before working the item. Sabotage: rebuild the details dict by hand
    with one shared key left out and this fails naming it."""
    created = _ok(_mcp(client, key, "create_item", {
        "title": "ratchet", "status": "next", "touchpoints": ["web/src/x.tsx"], "effort": 3}))
    item = db.get(Item, keys.resolve_item(db, created["id"]) or created["id"])
    assert item is not None
    shared = items_svc.item_dict(item)
    details = items_svc.get_item_details(db, item.id)
    missing = sorted(set(shared) - set(details))
    assert not missing, f"get_item_details dropped shared fields: {missing}"
    # And the values agree — a renamed key would pass the set test and still mislead.
    for k, v in shared.items():
        assert details[k] == v, k
