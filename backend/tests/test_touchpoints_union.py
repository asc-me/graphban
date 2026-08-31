"""Measured touchpoints union with declared ones; empty is not a write (GRPH-611 / P30 D10).

`update_item(touchpoints=[...])` used to assign the incoming list over the stored one.
An empty write then read as "this item collides with nothing", which is the absence that
looks like a clean partition. The client sends this reap's measured paths only; the
server keeps what was already there.
"""
from __future__ import annotations

from app.db import SessionLocal
from app.services import collision
from app.services import items as items_svc


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


def _item(db, **kw):
    return items_svc.create_item(db, project_id="core", title="Touch work", **kw)


# ---- the callee ------------------------------------------------------------------


def test_a_measured_write_does_not_erase_declared_paths(client):
    """THE ONE THAT MATTERS. Declared `app.py`, this reap measured `lib.py` — both stay.

    Before D10 the second write replaced the list, so the next partition forgot the
    declaration and two workers could be handed overlapping work that used to collide.
    """
    db = SessionLocal()
    try:
        item = _item(db, touchpoints=["backend/app/predicted.py"])
        items_svc.update_item(db, item.id, touchpoints=["backend/app/actual.py"])
        db.refresh(item)
        assert item.touchpoints == [
            "backend/app/predicted.py",
            "backend/app/actual.py",
        ], "replace-not-union: the declaration vanished"
    finally:
        db.close()


def test_an_empty_write_leaves_declared_paths_in_place(client):
    """Zero files touched is not "no collision". Leave predicted as-is."""
    db = SessionLocal()
    try:
        item = _item(db, touchpoints=["backend/app/predicted.py"])
        items_svc.update_item(db, item.id, touchpoints=[])
        db.refresh(item)
        assert item.touchpoints == ["backend/app/predicted.py"]
    finally:
        db.close()


def test_an_empty_write_does_not_claim_the_item_has_no_areas(client, monkeypatch):
    """An item that never declared paths still uses predicted areas after `[]`.

    If empty were stored as actual, `touch_areas` would return `([], "actual")` and
    the next cluster would treat it as safe to parallelise with everything.
    """
    from app.services import code_graph
    monkeypatch.setattr(
        code_graph, "search_code",
        lambda db, q, pid, top_k=5: [(type("N", (), {"path": "backend/app/inferred.py"})(), 0.9)],
    )
    db = SessionLocal()
    try:
        item = _item(db, touchpoints=[])
        items_svc.update_item(db, item.id, touchpoints=[])
        db.refresh(item)
        areas, src = collision.touch_areas(db, item, "core")
        assert src == "predicted", "empty write became an actual of nothing"
        assert "backend/app/inferred.py" in areas
    finally:
        db.close()


def test_a_retry_does_not_double_the_same_path(client):
    db = SessionLocal()
    try:
        item = _item(db, touchpoints=["a.py"])
        items_svc.update_item(db, item.id, touchpoints=["a.py", "b.py"])
        items_svc.update_item(db, item.id, touchpoints=["b.py"])
        db.refresh(item)
        assert item.touchpoints == ["a.py", "b.py"]
    finally:
        db.close()


def test_union_survives_a_round_trip_through_the_row(client):
    """SQLAlchemy JSON mutation tracking misses in-place appends. Assigning a new
    list is what makes the union actually persist — a sabotage that `.append`s
    in place would pass an in-memory check and lose `b.py` after refresh."""
    db = SessionLocal()
    try:
        item = _item(db, touchpoints=["a.py"])
        items_svc.update_item(db, item.id, touchpoints=["b.py"])
        db.expire(item)
        db.refresh(item)
        assert item.touchpoints == ["a.py", "b.py"]
    finally:
        db.close()


# ---- the call (MCP) ----------------------------------------------------------


def test_mcp_update_item_unions_touchpoints(client, auth):
    """Sabotage the CALL, not only the callee. A correct `union_touchpoints` that
    `update_item` never consults is a function nobody calls."""
    key = client.post("/api/api-keys", json={"name": "tp", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    made = _ok(client, key, "create_item",
               {"title": "declared", "touchpoints": ["backend/app/predicted.py"]})
    out = _ok(client, key, "update_item",
              {"id": made["id"], "touchpoints": ["backend/app/actual.py"]})
    assert "backend/app/predicted.py" in out["touchpoints"]
    assert "backend/app/actual.py" in out["touchpoints"]


def test_mcp_empty_touchpoints_is_not_a_write(client, auth):
    key = client.post("/api/api-keys", json={"name": "tp-empty", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    made = _ok(client, key, "create_item",
               {"title": "declared", "touchpoints": ["backend/app/predicted.py"]})
    out = _ok(client, key, "update_item",
              {"id": made["id"], "touchpoints": []})
    assert out["touchpoints"] == ["backend/app/predicted.py"]
