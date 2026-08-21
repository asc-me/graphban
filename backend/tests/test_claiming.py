"""Feature A: agent task claiming + leases."""
import json
from datetime import timedelta

from app.db import SessionLocal
from app.models import utcnow
from app.services import items as items_svc


def _mcp(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return r.json()["result"]


def test_claim_assigns_and_two_agents_never_collide(client):
    db = SessionLocal()
    a = items_svc.claim_next(db, "agent-1", project_id="core")
    b = items_svc.claim_next(db, "agent-2", project_id="core")
    assert a is not None and b is not None
    assert a.id != b.id  # the second claim can't take the first agent's item
    assert a.status == "in_progress" and a.claimed_by == "agent-1" and a.assignee == "agent-1"
    assert b.claimed_by == "agent-2"
    db.close()


def test_claim_skips_blocked(client):
    db = SessionLocal()
    top = items_svc._ready_candidates(db, "core", 600)[0]
    items_svc.update_item(db, top.id, blocker="waiting on infra")
    got = items_svc.claim_next(db, "agent-x", project_id="core")
    assert got is not None and got.id != top.id
    db.close()


def test_stale_claim_is_reclaimed(client):
    db = SessionLocal()
    it = items_svc.claim_next(db, "dead-agent", project_id="core")
    # Simulate the agent dying: backdate its lease well past the window.
    it.claimed_at = utcnow() - timedelta(seconds=10_000)
    db.commit()
    # Fresh claims take live-lease items first; drain until the abandoned one is picked back up.
    seen = []
    while True:
        got = items_svc.claim_next(db, "fresh-agent", project_id="core", lease_seconds=600)
        if got is None:
            break
        seen.append(got.id)
        if got.id == it.id:
            assert got.claimed_by == "fresh-agent"  # the abandoned item was reclaimed
            break
    assert it.id in seen
    db.close()


def test_heartbeat_and_release(client):
    db = SessionLocal()
    it = items_svc.claim_next(db, "agent-h", project_id="core")
    before = it.claimed_at
    # Non-holder can't heartbeat.
    assert items_svc.heartbeat(db, it.id, "someone-else") is None
    # Holder extends the lease.
    hb = items_svc.heartbeat(db, it.id, "agent-h")
    assert hb is not None and hb.claimed_at >= before
    # Release returns it to the queue.
    rel = items_svc.release_item(db, it.id, "agent-h")
    assert rel.status == "next" and rel.claimed_by is None and rel.assignee == ""
    db.close()


def test_mcp_claim_heartbeat_release(client, auth):
    key = client.post("/api/api-keys", json={"name": "loop-agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]

    claim = _mcp(client, key, "claim_next", {})["structuredContent"]
    assert claim["claimed"] is True
    item = claim["item"]
    # `key:` marks a row stamped by the CREDENTIAL rather than by a registered agent
    # (GRPH-437). Before the mark, this column could hold either with nothing separating them.
    assert item["status"] == "in_progress" and item["claimed_by"] == "key:loop-agent"

    hb = _mcp(client, key, "heartbeat", {"id": item["id"]})["structuredContent"]
    assert hb["id"] == item["id"]

    rel = _mcp(client, key, "release_item", {"id": item["id"]})["structuredContent"]
    assert rel["status"] == "next"

    # A second key can't heartbeat the first agent's (now released) item.
    other = client.post("/api/api-keys", json={"name": "other", "project_id": "core"},
                        headers=auth).json()["plaintext"]
    err = _mcp(client, other, "heartbeat", {"id": item["id"]})
    assert err["isError"] is True


def test_claim_next_empty_returns_null_item(client, auth):
    # A brand-new project with no items: claim finds nothing.
    client.post("/api/projects", json={"name": "Empty"}, headers=auth)
    key = client.post("/api/api-keys", json={"name": "e", "project_id": "empty"},
                      headers=auth).json()["plaintext"]
    res = _mcp(client, key, "claim_next", {})["structuredContent"]
    assert res == {"claimed": False, "item": None}
