"""GRPH-752 — the reviewer's pointer to the branch that carries the work.

PRD-17 D3 calls `Item.branch` the handoff: "Where the work landed. Travels to the reviewer,
who otherwise cannot see the diff." The column existed and nothing filled it, so a reviewer
read the base branch and reported work as absent that was sitting on the remote.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Agent, Item
from app.services import items as items_svc


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
    return client.post("/api/projects", json={"name": "Handoff"}, headers=auth).json()["id"]


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


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _item(client, key, title="work", **kw) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": ["backend/app/x.py"], **kw}))["id"]


def _stored(db, item_id) -> Item:
    db.expire_all()
    return db.get(Item, item_id)


def test_a_claim_records_the_branch_the_claimant_registered_with(client, key, db):
    """The finding, as a test. Sabotage: drop the values assignment and the reviewer is back
    to reading the base branch."""
    who = _agent(client, key, "worker", worktree="/w/one", branch="gb/wave-1")
    item = _item(client, key)
    assert items_svc.claim_item(db, item, who) is not None
    assert _stored(db, item).branch == "gb/wave-1"


def test_an_agent_that_reports_no_branch_never_clears_one(client, key, db):
    """Losing the pointer is the defect, not the fix. Sabotage: write the branch
    unconditionally and this fails."""
    first = _agent(client, key, "first", worktree="/w/one", branch="gb/wave-1")
    item = _item(client, key)
    assert items_svc.claim_item(db, item, first) is not None
    assert _stored(db, item).branch == "gb/wave-1"

    # Released, then taken by an agent that registered without a branch.
    stored = _stored(db, item)
    stored.claimed_by, stored.claimed_at, stored.status = None, None, "next"
    db.commit()
    second = _agent(client, key, "second", worktree="/w/two")
    assert items_svc.claim_item(db, item, second) is not None
    assert _stored(db, item).branch == "gb/wave-1", "the only pointer there was got erased"


def test_the_branch_moves_with_the_claimant(client, key, db):
    """It follows the holder for the same reason `built_by` does: the work will land on THIS
    agent's branch, and a stale pointer sends the reviewer to somebody else's diff."""
    first = _agent(client, key, "first", worktree="/w/one", branch="gb/wave-1")
    item = _item(client, key)
    assert items_svc.claim_item(db, item, first) is not None

    stored = _stored(db, item)
    stored.claimed_by, stored.claimed_at, stored.status = None, None, "next"
    db.commit()
    second = _agent(client, key, "second", worktree="/w/two", branch="gb/wave-2")
    assert items_svc.claim_item(db, item, second) is not None
    assert _stored(db, item).branch == "gb/wave-2"


def test_every_claim_path_records_it_not_just_the_one(client, key, db, proj):
    """`_try_claim` is documented as the single write point for every claim. This is the
    assertion rather than the docstring that says so: `claim_next` and `claim_cluster` reach
    it by different routes, and a guarantee that holds on one of three paths is not one."""
    who = _agent(client, key, "worker", worktree="/w/one", branch="gb/wave-9")
    _item(client, key, "for claim_next")
    got = _ok(_mcp(client, key, "claim_next", {"agent_id": who}))
    assert got.get("claimed") and got.get("item"), got
    assert _stored(db, got["item"]["id"]).branch == "gb/wave-9"

    other = _agent(client, key, "cluster", worktree="/w/two", branch="gb/wave-10")
    _item(client, key, "for claim_cluster", touchpoints=["backend/app/other.py"])
    cluster = _ok(_mcp(client, key, "claim_cluster", {"agent_id": other, "max_items": 1}))
    ids = [i["id"] if isinstance(i, dict) else i for i in (cluster.get("items") or [])]
    assert ids, cluster
    for got_id in ids:
        assert _stored(db, got_id).branch == "gb/wave-10"


def test_the_reviewer_reads_the_branch_off_the_item(client, key, db):
    """What the whole column is for: an item in review names where its diff is.

    Both routes, because a reviewer reaches an item two ways. Sabotage: leave `branch` out of
    the item payload and a reviewer reading the item directly is back to guessing.
    """
    # Distinct `capabilities.instance` on both: review independence is decided on the call
    # tree, and two agents that declare nothing are indistinguishable to it.
    who = _agent(client, key, "worker", worktree="/w/one", branch="gb/wave-3",
                 capabilities={"instance": "builder"})
    item = _item(client, key)
    assert items_svc.claim_item(db, item, who) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": who}))

    details = _ok(_mcp(client, key, "get_item_details", {"id": item}))
    assert details.get("branch") == "gb/wave-3", details.get("branch")

    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "rev"})
    claimed = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert claimed.get("claimed") is True, claimed
    assert claimed.get("branch") == "gb/wave-3", claimed


def test_an_item_nobody_has_claimed_says_it_has_no_branch_rather_than_omitting_the_field(
        client, key, db):
    """An absent key reads as "this system has no such concept"; an empty one reads as "not
    yet known", which is the truth."""
    item = _item(client, key)
    details = _ok(_mcp(client, key, "get_item_details", {"id": item}))
    assert "branch" in details and details["branch"] == ""
