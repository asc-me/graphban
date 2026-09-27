"""Feature B: touchpoints + related-work clustering."""
from app.db import SessionLocal
from app.models import Link
from app.services import clustering as cluster_svc
from app.services import items as items_svc
from sqlalchemy import select


def _mk(db, title, tps, status="next"):
    return items_svc.create_item(db, title=title, project_id="core", status=status, touchpoints=tps)


def test_related_items_by_shared_touchpoints_and_glob(client):
    db = SessionLocal()
    a = _mk(db, "router work", ["backend/app/routers/items.py"])
    b = _mk(db, "same file", ["backend/app/routers/items.py", "backend/app/schemas/__init__.py"])
    c = _mk(db, "glob over routers", ["backend/app/routers/*"])
    d = _mk(db, "unrelated", ["web/src/App.tsx"])

    rel = cluster_svc.related_items(db, a, "core")
    ids = [r["item"].id for r in rel]
    assert b.id in ids and c.id in ids  # exact + glob both match
    assert d.id not in ids
    # b shares the exact file → surfaced with the shared touchpoint recorded
    b_row = next(r for r in rel if r["item"].id == b.id)
    assert "backend/app/routers/items.py" in b_row["shared"]
    db.close()


def test_touchpoints_auto_create_code_links(client):
    db = SessionLocal()
    a = _mk(db, "svc a", ["backend/app/services/items.py"])
    b = _mk(db, "svc b", ["backend/app/services/items.py"])  # shares → link created on create
    links = db.scalars(select(Link).where(Link.type == "code")).all()
    pair = [(l.a, l.b) for l in links]
    assert (b.id, a.id) in pair or (a.id, b.id) in pair
    db.close()


def test_next_cluster_claims_neighbourhood(client, auth):
    # Isolate the cluster in its own project so the seed is one of these items.
    client.post("/api/projects", json={"name": "Cluster"}, headers=auth)  # id: cluster
    db = SessionLocal()
    for title, tp in [
        ("seed", "backend/app/routers/auth.py"),
        # A directory beside the seed: two NAMED files in one folder no longer relate
        # (GRPH-951), so the neighbourhood is declared the vague way.
        ("near", "backend/app/routers/items"),   # same dir
        ("near2", "backend/app/routers/auth.py"),  # same file
    ]:
        items_svc.create_item(db, title=title, project_id="cluster", status="next", touchpoints=[tp])
    batch = cluster_svc.next_cluster(db, "agent-c", project_id="cluster", max_items=3)
    assert len(batch) >= 2  # seed + at least one neighbour claimed together
    assert all(x["item"].claimed_by == "agent-c" and x["item"].status == "in_progress" for x in batch)
    assert batch[0]["seed"] is True
    db.close()


def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, headers={"X-API-Key": key})
    return r.json()["result"]


def test_mcp_related_work_and_next_cluster(client, auth):
    key = client.post("/api/api-keys", json={"name": "cluster-agent", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    # two items on the same file
    _mcp(client, key, "create_item", {"title": "tp one", "status": "next",
                                       "touchpoints": ["backend/app/services/memory.py"]})
    two = _mcp(client, key, "create_item", {"title": "tp two", "status": "next",
                                            "touchpoints": ["backend/app/services/memory.py"]})["structuredContent"]

    rel = _mcp(client, key, "related_work", {"id": two["id"]})["structuredContent"]
    assert any("backend/app/services/memory.py" in r["shared"] for r in rel["results"])

    cluster = _mcp(client, key, "next_cluster", {"max_items": 2})["structuredContent"]
    assert cluster["claimed"] >= 1
    assert cluster["cluster"][0]["seed"] is True
