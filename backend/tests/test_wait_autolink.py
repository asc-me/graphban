"""GRPH-1650: a decision wait filed by a claim holder is linked, blocked and never claimable."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.models import Link, utcnow
from app.services import items as items_svc
from app.services import waits as waits_svc


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _A:
    id = "GRPH-A1650"


def _holder(db):
    item = items_svc.create_item(db, project_id="core", title="work", status="in_progress")
    item.claimed_by, item.claimed_at = "GRPH-A1650", utcnow()
    db.commit()
    return _A, item


def test_claim_holder_question_is_linked_and_blocks(db):
    agent, item = _holder(db)
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 options=["a", "b"])
    out = waits_svc.attach_to_claim(db, wait, agent.id)
    db.refresh(item)
    assert out["linked"] is True
    assert wait.status == "blocked"
    assert item.status == "blocked" and item.blocker == wait.id
    link = db.query(Link).filter(Link.a == item.id, Link.b == wait.id).one()
    assert link.type == "dependency" and link.reason == "wait:decision"
    assert waits_svc.question_state(db, item) is not None


def test_no_claim_keeps_old_behaviour(db):
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?")
    out = waits_svc.attach_to_claim(db, wait, None)
    assert out["linked"] is False and "no ticket was linked" in out["note"]
    assert wait.status == "backlog"
    assert db.query(Link).filter(Link.b == wait.id).count() == 0


def test_wait_decision_is_never_claimable(db):
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 status="next")
    assert items_svc.claimable(wait) is False
    got = items_svc.claim_next(db, "GRPH-A1651", project_id="core")
    assert got is None or got.id != wait.id


# --- over the wire: the caller is resolved, never trusted from a schema it was not shown ---

import json  # noqa: E402

from app.models import Item  # noqa: E402
from app.services import clustering as clustering_svc  # noqa: E402


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "WaitAutolink"},
                       headers=auth).json()["id"]


def _key(client, auth, proj, name="fleet"):
    return client.post("/api/api-keys", json={"name": name, "project_id": proj},
                       headers=auth).json()["plaintext"]


def _rpc(client, key, method, params=None, sid=None):
    headers = {"X-API-Key": key}
    if sid:
        headers["Mcp-Session-Id"] = sid
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params or {}}, headers=headers)


def _tool(client, key, name, args, sid=None):
    r = _rpc(client, key, "tools/call", {"name": name, "arguments": args}, sid=sid).json()
    return json.loads(r["result"]["content"][0]["text"])


def _holding_agent(client, key, proj):
    """A registered agent on its own connection, holding an in_progress claim."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    me = _tool(client, key, "register_agent", {"label": "W"}, sid=sid)["agent_id"]
    work = _tool(client, key, "create_item", {"title": "work", "status": "next"}, sid=sid)
    got = _tool(client, key, "claim_next", {"agent_id": me}, sid=sid)
    assert got["item"]["id"] == work["id"]
    return sid, me, work["id"]


def test_create_item_agent_id_is_fleet_only():
    """The core manifest has no headroom: `agent_id` is advertised to the fleet tier only.
    A core caller is resolved from the connection (see the no-agent_id test below)."""
    from app.mcp_server import TOOLS, _with_fleet_args
    core = next(t for t in TOOLS if t["name"] == "create_item")
    assert "agent_id" not in core["inputSchema"]["properties"]
    fleet = next(t for t in _with_fleet_args(TOOLS) if t["name"] == "create_item")
    assert "agent_id" in fleet["inputSchema"]["properties"]


def test_question_over_mcp_without_agent_id_links_the_held_claim(client, auth, proj, db):
    """Acceptance clause 1's exact scenario: a schema-following child sends no agent_id."""
    key = _key(client, auth, proj)
    sid, _me, work_id = _holding_agent(client, key, proj)

    out = _tool(client, key, "create_item",
                {"title": "which?", "question": "a or b?", "options": ["a", "b"]}, sid=sid)

    assert out["wait_link"]["linked"] is True
    assert out["status"] == "blocked"
    held = db.get(Item, work_id)
    assert held.status == "blocked" and held.blocker == out["id"]
    link = db.query(Link).filter(Link.a == work_id, Link.b == out["id"]).one()
    assert link.type == "dependency" and link.reason == "wait:decision"


def test_wait_decision_tag_alone_links_too(client, auth, proj, db):
    key = _key(client, auth, proj)
    sid, _me, work_id = _holding_agent(client, key, proj)
    out = _tool(client, key, "create_item", {"title": "q", "tags": ["wait:decision"]}, sid=sid)
    assert out["wait_link"]["linked"] is True
    assert db.get(Item, work_id).blocker == out["id"]


def test_question_with_no_claim_over_mcp_says_nothing_was_linked(client, auth, proj, db):
    key = _key(client, auth, proj)
    out = _tool(client, key, "create_item", {"title": "q", "question": "which?"})
    assert out["wait_link"]["linked"] is False
    assert "no ticket was linked" in out["wait_link"]["note"]
    assert out["status"] == "backlog"
    assert db.query(Link).filter(Link.b == out["id"]).count() == 0


def test_another_credentials_agent_id_blocks_nothing(client, auth, proj, db):
    """An agent_id is honoured only on the credential that registered it."""
    owner = _key(client, auth, proj, "owner")
    _sid, victim, work_id = _holding_agent(client, owner, proj)
    other = _key(client, auth, proj, "other")

    out = _tool(client, other, "create_item",
                {"title": "q", "question": "which?", "agent_id": victim})

    assert out["wait_link"]["linked"] is False
    held = db.get(Item, work_id)
    assert held.status == "in_progress" and not held.blocker


def test_plain_item_carries_no_wait_link(client, auth, proj):
    key = _key(client, auth, proj)
    out = _tool(client, key, "create_item", {"title": "plain"})
    assert "wait_link" not in out


def test_next_cluster_never_offers_a_wait(db):
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 status="next")
    batch = clustering_svc.next_cluster(db, "GRPH-A1652", "core", max_items=5)
    assert wait.id not in {row["item"].id for row in batch}


def test_a_wait_cannot_be_delegated(db):
    from app.models import Agent
    from app.services import delegation as delegation_svc

    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 status="next")
    agent = Agent(id="core-a1650", project_id="core", number=1650, label="planner")
    db.add(agent)
    db.commit()
    with pytest.raises(Exception, match="not ready"):
        delegation_svc.delegate(db, agent=agent, item=wait, lane="backend", tier="cheap",
                              lease_seconds=items_svc.DEFAULT_LEASE_SECONDS)
