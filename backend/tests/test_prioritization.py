"""Feature C: dependency-aware prioritization."""
import json

from app.db import SessionLocal
from app.services import items as items_svc
from app.services import prioritization as prio
from tests import attest


def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, headers={"X-API-Key": key})
    return r.json()["result"]


def _mk(db, title, status="next", effort=0, project="prio"):
    return items_svc.create_item(db, title=title, project_id=project, status=status, effort=effort)


def test_dependency_blocks_readiness(client, auth):
    client.post("/api/projects", json={"name": "Prio"}, headers=auth)
    db = SessionLocal()
    dep = _mk(db, "must ship first", status="backlog")
    blocked = _mk(db, "waits on dep")
    from app.services import links as links_svc
    links_svc.create_link(db, a=blocked.id, b=dep.id, type_="dependency", project_id="prio")

    ctx = prio.context(db, "prio")
    assert prio.blocked_by(ctx, blocked) == [dep.id]     # blocked until dep is done
    assert prio.ready(ctx, blocked) is False
    assert prio.ready(ctx, dep) is True
    assert prio.unblocks(ctx, dep) == 1                  # dep unblocks the other item

    # suggest_next / claim never hand out a blocked item.
    assert items_svc.suggest_next(db, project_id="prio").id == dep.id
    claimed = items_svc.claim_next(db, "agent", project_id="prio")
    assert claimed.id == dep.id

    # Finish the dependency → the blocked item becomes ready and claimable.
    attest.complete(db, dep.id)
    assert prio.ready(prio.context(db, "prio"), blocked) is True
    assert items_svc.claim_next(db, "agent2", project_id="prio").id == blocked.id
    db.close()


def test_fanout_and_votes_rank_higher(client, auth):
    client.post("/api/projects", json={"name": "Rank"}, headers=auth)
    db = SessionLocal()
    hub = _mk(db, "unblocks several", status="next", effort=8, project="rank")
    plain = _mk(db, "isolated task", status="next", effort=1, project="rank")
    from app.services import links as links_svc
    for _ in range(3):
        leaf = _mk(db, "leaf", status="backlog", project="rank")
        links_svc.create_link(db, a=leaf.id, b=hub.id, type_="dependency", project_id="rank")

    ctx = prio.context(db, "rank")
    assert prio.score(ctx, hub) > prio.score(ctx, plain)  # fan-out beats low effort
    # The hub sorts first even though it's higher-effort.
    ranked = prio.prioritized(db, "rank", statuses=("next",))
    assert ranked[0]["item"].id == hub.id and ranked[0]["unblocks"] == 3
    db.close()


def test_mcp_get_backlog_carries_priority_metadata(client, auth):
    key = client.post("/api/api-keys", json={"name": "planner", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    bl = _mcp(client, key, "get_backlog", {})["structuredContent"]
    assert "total" in bl and bl["results"]
    top = bl["results"][0]
    assert {"ready", "blocked_by", "unblocks", "votes", "score"} <= set(top)
