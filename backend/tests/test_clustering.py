"""Feature B: touchpoints + related-work clustering."""
from app.db import SessionLocal
from app.models import Item, Link
from app.services import clustering as cluster_svc
from app.services import items as items_svc
from sqlalchemy import func, or_, select


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


# ---- GRPH-1068: code links reach open work only, and are pruned on completion ----

_ATTESTATION = {
    "kind": "attestation", "adapter": "github-actions", "commit": "a" * 40,
    "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}],
}


def _force_done(db, item):
    """Done without going through update_item — history that predates the prune."""
    item.status = "done"
    db.commit()


def _complete(db, item):
    return items_svc.update_item(db, item.id, status="done", evidence=[_ATTESTATION])


def _types_on(db, item_id):
    rows = db.scalars(select(Link).where(or_(Link.a == item_id, Link.b == item_id))).all()
    return sorted(l.type for l in rows)


def _code_peers(db, item_id):
    rows = db.scalars(select(Link).where(
        Link.type == "code", or_(Link.a == item_id, Link.b == item_id))).all()
    return {l.b if l.a == item_id else l.a for l in rows}


def test_a_new_item_code_links_open_overlap_only_not_done(client):
    db = SessionLocal()
    tp = ["backend/app/services/grph1068_a.py"]
    finished = _mk(db, "finished", tp)
    _force_done(db, finished)
    db.execute(Link.__table__.delete().where(Link.type == "code"))
    db.commit()
    live = _mk(db, "live", tp)
    new = _mk(db, "new", tp)

    assert _code_peers(db, new.id) == {live.id}
    db.close()


def test_completing_an_item_drops_its_code_links_and_keeps_authored_ones(client):
    from app.services import links as links_svc

    db = SessionLocal()
    tp = ["backend/app/services/grph1068_b.py"]
    item = _mk(db, "to finish", tp)
    peer = _mk(db, "peer", tp)  # code link, made on create
    dep = _mk(db, "dep", ["web/src/grph1068_dep.tsx"])
    sem = _mk(db, "sem", ["web/src/grph1068_sem.tsx"])
    links_svc.create_link(db, a=item.id, b=dep.id, type_="dependency", project_id="core")
    links_svc.create_link(db, a=sem.id, b=item.id, type_="semantic", project_id="core")
    assert _types_on(db, item.id) == ["code", "dependency", "semantic"]

    _complete(db, item)

    assert _types_on(db, item.id) == ["dependency", "semantic"]
    assert peer.id not in _code_peers(db, item.id)
    db.close()


def test_reopening_a_done_item_relinks_it_to_open_overlap(client):
    db = SessionLocal()
    tp = ["backend/app/services/grph1068_c.py"]
    item = _mk(db, "reopen me", tp)
    _complete(db, item)
    assert _code_peers(db, item.id) == set()
    peer = _mk(db, "opened while it was done", tp)  # skips the done item
    assert _code_peers(db, item.id) == set()

    items_svc.update_item(db, item.id, status="next")

    assert _code_peers(db, item.id) == {peer.id}
    db.close()


def test_the_migration_sweep_removes_code_links_touching_done_items_only(client):
    import importlib.util
    import pathlib

    from app.db import engine
    from app.services import links as links_svc

    path = pathlib.Path(__file__).parents[1] / "alembic/versions/0138_prune_done_code_links.py"
    spec = importlib.util.spec_from_file_location("mig_0138", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    db = SessionLocal()
    done, open_a, open_b = (_mk(db, t, ["web/src/grph1068_mig/" + t]) for t in ("d", "a", "b"))
    _force_done(db, done)
    kept = [
        links_svc.create_link(db, a=open_a.id, b=open_b.id, type_="code", project_id="core"),
        links_svc.create_link(db, a=open_a.id, b=done.id, type_="dependency", project_id="core"),
        links_svc.create_link(db, a=done.id, b=open_b.id, type_="semantic", project_id="core"),
    ]
    gone = [
        links_svc.create_link(db, a=open_a.id, b=done.id, type_="code", project_id="core"),
        links_svc.create_link(db, a=done.id, b=open_b.id, type_="code", project_id="core"),
    ]
    stale = db.scalar(select(func.count()).select_from(Link).where(
        Link.type == "code",
        or_(Link.a.in_(select(Item.id).where(Item.status == "done")),
            Link.b.in_(select(Item.id).where(Item.status == "done")))))
    ids_before = {l.id for l in db.scalars(select(Link)).all()}
    db.close()

    with engine.begin() as conn:
        removed = mig.sweep(conn)

    db = SessionLocal()
    ids_after = {l.id for l in db.scalars(select(Link)).all()}
    assert removed == stale >= len(gone)
    assert not any(l.id in ids_after for l in gone)
    assert all(l.id in ids_after for l in kept)
    assert len(ids_before - ids_after) == removed
    db.close()


def test_related_work_still_finds_a_done_item_by_live_overlap(client):
    db = SessionLocal()
    tp = ["backend/app/services/grph1068_e.py"]
    finished = _mk(db, "history", tp)
    _complete(db, finished)
    item = _mk(db, "now", tp)
    assert finished.id not in _code_peers(db, item.id)

    rel = cluster_svc.related_items(db, item, "core")

    row = next(r for r in rel if r["item"].id == finished.id)
    assert tp[0] in row["shared"]
    db.close()


def test_collision_clusters_unchanged_by_code_link_pruning(client):
    from app.services import collision

    db = SessionLocal()
    a = _mk(db, "ca", ["backend/app/services/grph1068_f.py"])
    b = _mk(db, "cb", ["backend/app/services/grph1068_f.py"])
    c = _mk(db, "cc", ["web/src/grph1068_g.tsx"])
    done = _mk(db, "cd", ["backend/app/services/grph1068_f.py"])
    open_items = [a, b, c]

    before = collision.collision_clusters(db, open_items, "core")
    _complete(db, done)
    db.execute(Link.__table__.delete().where(Link.type == "code"))
    db.commit()
    after = collision.collision_clusters(db, open_items, "core")

    norm = lambda cs: sorted((sorted(x["items"]), sorted(x["areas"]), x["collides"]) for x in cs)
    assert norm(before) == norm(after)
    assert [sorted(x["items"]) for x in after if x["collides"]] == [sorted([a.id, b.id])]
    db.close()
