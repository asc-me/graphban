"""GRPH-1843 — update_item refuses review when the branch name is the gitops base.

The server has no git. "Equals its base" is the measured `gitops_base_branch`.
An unset base is not a match, and a person moving a card is not gated.
"""
from __future__ import annotations

import pytest

from app.models import Item, Project
from app.services import items as items_svc


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Empty"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


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


def _refused(res) -> str:
    assert res.get("isError"), res
    return res["content"][0]["text"]


def _item(client, key) -> str:
    return _ok(_mcp(client, key, "create_item",
                    {"title": "empty branch", "status": "in_progress"}))["id"]


def _set_branch(db, item_id: str, branch: str) -> Item:
    stored = db.get(Item, items_svc.keys.resolve_item(db, item_id) or item_id)
    stored.branch = branch
    db.commit()
    return stored


def test_review_is_refused_when_the_branch_equals_the_gitops_base(client, key, db, proj):
    """The reason names the branch. Sabotage: drop the branch_equals_base check
    in update_item and this item reaches review."""
    db.get(Project, proj).gitops_base_branch = "main"
    db.commit()
    who = _ok(_mcp(client, key, "register_agent",
                   {"branch": "gb/feature", "label": "builder"}))["agent_id"]
    item = _item(client, key)
    _set_branch(db, item, "main")
    text = _refused(_mcp(client, key, "update_item",
                         {"id": item, "status": "review", "agent_id": who}))
    assert "`main`" in text
    db.expire_all()
    assert db.get(Item, items_svc.keys.resolve_item(db, item)).status == "in_progress"


def test_origin_main_equals_a_base_named_main(client, key, db, proj):
    db.get(Project, proj).gitops_base_branch = "main"
    db.commit()
    who = _ok(_mcp(client, key, "register_agent",
                   {"branch": "gb/feature", "label": "builder"}))["agent_id"]
    item = _item(client, key)
    _set_branch(db, item, "origin/main")
    text = _refused(_mcp(client, key, "update_item",
                         {"id": item, "status": "review", "agent_id": who}))
    assert "origin/main" in text


def test_a_feature_branch_still_moves_to_review(client, key, db, proj):
    db.get(Project, proj).gitops_base_branch = "main"
    db.commit()
    who = _ok(_mcp(client, key, "register_agent",
                   {"branch": "gb/feature", "label": "builder"}))["agent_id"]
    item = _item(client, key)
    _set_branch(db, item, "gb/feature")
    _ok(_mcp(client, key, "update_item",
             {"id": item, "status": "review", "agent_id": who}))
    db.expire_all()
    assert db.get(Item, items_svc.keys.resolve_item(db, item)).status == "review"


def test_an_unset_gitops_base_does_not_refuse_a_branch_named_main(client, key, db, proj):
    """Absence is not a match. An unmeasured project does not treat `main` as its base."""
    assert db.get(Project, proj).gitops_base_branch in (None, "")
    who = _ok(_mcp(client, key, "register_agent",
                   {"branch": "gb/feature", "label": "builder"}))["agent_id"]
    item = _item(client, key)
    _set_branch(db, item, "main")
    _ok(_mcp(client, key, "update_item",
             {"id": item, "status": "review", "agent_id": who}))


def test_a_person_moving_a_card_is_not_gated(client, auth, db, proj):
    db.get(Project, proj).gitops_base_branch = "main"
    db.commit()
    made = client.post("/api/items", json={"title": "by hand", "project_id": proj},
                       headers=auth).json()
    _set_branch(db, made["id"], "main")
    r = client.patch(f"/api/items/{made['id']}", json={"status": "review"}, headers=auth)
    assert r.status_code == 200, r.text
