"""GRPH-1031. `update_item(branch=…)` is identity, planner/supervisor only.

A worker rewriting the branch under a live reviewer is how GRPH-946's "reviewer has
nothing to read" comes back. Empty is not a write: a PATCH that sends `branch=""`
must not clear a pointer someone is holding.
"""
from __future__ import annotations

import inspect

import pytest

from app.mcp_server import _FLEET_ONLY_ARGS
from app.services import fleet as fleet_svc
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


def _refused(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert res.get("isError") is True, res
    return res["structuredContent"]["error"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def agent_key(client, auth):
    return client.post("/api/api-keys", json={"name": "fleet"},
                       headers=auth).json()["plaintext"]


def _register(client, key, role="worker"):
    return _ok(client, key, "register_agent",
               {"branch": "gb/test", "label": f"{role} term", "role_hint": role})


def test_a_planner_can_retarget_the_branch(client, agent_key, db):
    """THE write. Sabotage: drop `branch=args.get("branch")` from the handler —
    this returns 200 and the field is still empty."""
    me = _register(client, agent_key, "planner")
    item = _ok(client, agent_key, "create_item",
               {"title": "retarget me", "status": "next"})

    out = _ok(client, agent_key, "update_item",
              {"id": item["id"], "branch": "gb/bounced-head", "agent_id": me["agent_id"]})

    assert out.get("branch") == "gb/bounced-head"
    stored = items_svc.get_item(db, item["id"])
    db.refresh(stored)
    assert stored.branch == "gb/bounced-head"


def test_a_worker_cannot_retarget_the_branch(client, agent_key, db):
    """THE refusal. Sabotage: drop the branch clause from check_tool_role — a
    worker rewrites identity and this stays green."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    before = items_svc.get_item(db, item_key)
    db.refresh(before)
    held = before.branch

    err = _refused(client, agent_key, "update_item",
                   {"id": item_key, "branch": "gb/stolen", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "worker" in err["message"]
    after = items_svc.get_item(db, item_key)
    db.refresh(after)
    assert after.branch == held
    assert after.branch != "gb/stolen"


def test_an_empty_write_does_not_clear_a_recorded_branch(client, agent_key, db):
    """Same rule as touchpoints. Sabotage: `if key in fields: setattr` for branch
    — `branch=""` wipes the pointer a reviewer is holding."""
    me = _register(client, agent_key, "planner")
    item = _ok(client, agent_key, "create_item", {"title": "keep the pointer", "status": "next"})
    _ok(client, agent_key, "update_item",
        {"id": item["id"], "branch": "gb/keep-me", "agent_id": me["agent_id"]})

    out = _ok(client, agent_key, "update_item",
              {"id": item["id"], "branch": "", "agent_id": me["agent_id"]})

    assert out.get("branch") == "gb/keep-me"
    stored = items_svc.get_item(db, item["id"])
    db.refresh(stored)
    assert stored.branch == "gb/keep-me"


def test_a_jwt_empty_write_does_not_clear_either(client, auth, db):
    made = client.post("/api/items", json={"title": "human retarget", "status": "next"},
                       headers=auth).json()
    stored = items_svc.get_item(db, made["id"])
    stored.branch = "gb/from-human"
    db.commit()

    r = client.patch(f"/api/items/{made['id']}", json={"branch": ""}, headers=auth)
    assert r.status_code == 200, r.text
    db.refresh(stored)
    assert stored.branch == "gb/from-human"


def test_rewriting_the_branch_in_review_is_refused(client, agent_key, db):
    me = _register(client, agent_key, "planner")
    item = _ok(client, agent_key, "create_item", {"title": "in review", "status": "next"})
    _ok(client, agent_key, "update_item",
        {"id": item["id"], "branch": "gb/under-review", "agent_id": me["agent_id"]})
    stored = items_svc.get_item(db, item["id"])
    stored.status = "review"
    db.commit()

    with pytest.raises(ValueError, match="in review"):
        items_svc.update_item(db, item["id"], branch="gb/rewritten")
    db.refresh(stored)
    assert stored.branch == "gb/under-review"


def test_the_branch_write_is_on_the_call():
    """THE CALL (GRPH-534). Sabotage: `if False` on the worker branch clause —
    every other test can still pass if the handler never sends the field.

    Read from disk: inspect.getsource(_call_tool) is a pytest wrapper.
    """
    from pathlib import Path

    src = inspect.getsource(fleet_svc.check_tool_role)
    assert 'get("branch")' in src
    assert "planner/supervisor" in src
    mcp = (Path(__file__).resolve().parents[1] / "app" / "mcp_server.py").read_text(
        encoding="utf-8")
    assert "branch=args.get(\"branch\")" in mcp
    assert "update_item" in _FLEET_ONLY_ARGS
    assert "branch" in _FLEET_ONLY_ARGS["update_item"]
