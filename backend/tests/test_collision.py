"""AL-192: collision-aware clustering — actual/predicted touch-areas, non-colliding clusters."""
import types

import pytest

from app.db import SessionLocal
from app.services import code_graph, collision
from app.services import items as items_svc
from app.services import links as links_svc


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, title, touchpoints=None, desc=""):
    return items_svc.create_item(db, title=title, description=desc, project_id="core",
                                 touchpoints=touchpoints or [])


def _no_code_hits(monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])


def test_touch_areas_prefers_actual_touchpoints(db):
    it = _item(db, "Widget", touchpoints=["backend/app/widget.py"])
    areas, src = collision.touch_areas(db, it, "core")
    assert areas == ["backend/app/widget.py"] and src == "actual"


def test_predict_uses_code_map_inference_above_threshold(db, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [
        (types.SimpleNamespace(path="backend/app/sync.py"), 0.9),
        (types.SimpleNamespace(path="backend/app/weak.py"), 0.05)])  # below min-sim
    it = _item(db, "Fix the sync engine")
    areas, src = collision.touch_areas(db, it, "core")
    assert src == "predicted"
    assert "backend/app/sync.py" in areas and "backend/app/weak.py" not in areas


def test_predict_uses_linked_item_touchpoints_as_learned_signal(db, monkeypatch):
    _no_code_hits(monkeypatch)  # learned signal only
    known = _item(db, "Known area", touchpoints=["backend/app/auth.py"])
    fresh = _item(db, "Fresh ticket")  # no touchpoints
    links_svc.create_link(db, a=fresh.id, b=known.id, type_="dependency", project_id="core")
    areas, src = collision.touch_areas(db, fresh, "core")
    assert src == "predicted" and "backend/app/auth.py" in areas


def test_clusters_group_overlapping_and_split_independent(db, monkeypatch):
    _no_code_hits(monkeypatch)
    a = _item(db, "A", touchpoints=["backend/app/pay.py"])
    b = _item(db, "B", touchpoints=["backend/app/pay.py"])   # collides with A
    c = _item(db, "C", touchpoints=["web/src/cart.tsx"])     # independent
    clusters = collision.collision_clusters(db, [a, b, c], "core")

    ab = next(cl for cl in clusters if a.id in cl["items"])
    assert set(ab["items"]) == {a.id, b.id} and ab["collides"] is True and ab["predicted"] is False
    solo = next(cl for cl in clusters if c.id in cl["items"])
    assert solo["items"] == [c.id] and solo["collides"] is False
    assert clusters[0]["items"] == ab["items"]  # largest cluster first


def test_glob_or_directory_overlap_counts_as_collision(db, monkeypatch):
    _no_code_hits(monkeypatch)
    a = _item(db, "A", touchpoints=["backend/app/routers/items.py"])
    b = _item(db, "B", touchpoints=["backend/app/routers/*.py"])  # glob covers A
    clusters = collision.collision_clusters(db, [a, b], "core")
    assert len(clusters) == 1 and set(clusters[0]["items"]) == {a.id, b.id}


def test_predicted_cluster_is_flagged(db, monkeypatch):
    # both items lack touchpoints; a shared inferred path groups them, flagged predicted
    monkeypatch.setattr(code_graph, "search_code",
                        lambda db, q, pid, top_k=5: [(types.SimpleNamespace(path="backend/app/x.py"), 0.8)])
    a = _item(db, "infer A")
    b = _item(db, "infer B")
    clusters = collision.collision_clusters(db, [a, b], "core")
    assert len(clusters) == 1 and clusters[0]["collides"] and clusters[0]["predicted"] is True


def test_endpoint_returns_clusters_and_requires_auth(client, auth, monkeypatch):
    from app.services import code_graph as cg
    monkeypatch.setattr(cg, "search_code", lambda db, q, pid, top_k=5: [])

    a = client.post("/api/items", json={"title": "Pay A", "project_id": "core",
                    "touchpoints": ["backend/app/billing.py"]}, headers=auth).json()
    b = client.post("/api/items", json={"title": "Pay B", "project_id": "core",
                    "touchpoints": ["backend/app/billing.py"]}, headers=auth).json()

    assert client.get("/api/items/collision-clusters?project_id=core").status_code == 401
    r = client.get("/api/items/collision-clusters?project_id=core", headers=auth)
    assert r.status_code == 200
    pay = next(cl for cl in r.json()["clusters"] if a["id"] in cl["items"])
    assert {a["id"], b["id"]} <= set(pay["items"]) and pay["collides"] is True


def test_clusters_for_project_withholds_items_with_unmet_dependencies(db, monkeypatch):
    """GRPH-885: items whose blocked_by dependencies are unfinished are withheld from the
    partition. A cluster that includes a blocked item is a promise the divvy cannot keep."""
    _no_code_hits(monkeypatch)
    root = _item(db, "Root", touchpoints=["backend/app/spanner.py"])
    child = _item(db, "Child", touchpoints=["backend/app/spanner.py"])
    # child depends on root
    links_svc.create_link(db, a=child.id, b=root.id, type_="dependency", project_id="core")

    clusters = collision.clusters_for_project(db, "core")
    # Both items share touchpoints, so they would normally cluster together.
    # But child is blocked by root (not done), so only root should appear.
    all_items = [it for cl in clusters for it in cl["items"]]
    assert root.id in all_items
    assert child.id not in all_items


def test_clusters_for_project_withholds_items_with_manual_blocker(db, monkeypatch):
    """GRPH-885: items with a manual blocker field are also withheld."""
    _no_code_hits(monkeypatch)
    blocked = _item(db, "Blocked", touchpoints=["backend/app/x.py"])
    items_svc.update_item(db, blocked.id, blocker="Waiting on external API")

    clusters = collision.clusters_for_project(db, "core")
    all_items = [it for cl in clusters for it in cl["items"]]
    assert blocked.id not in all_items


def test_review_occupies_the_glob_after_its_reservation_drops(db, monkeypatch):
    """GRPH-886 CALL. Move the seed to review and drop its reservation. The sibling
    that shares the glob must not be startable: not in an unheld cluster, and not a
    member `claim_cluster` can take.

    Sabotage: delete the `_occupy_review_globs` call in `clusters_for_project`. This
    fails — the sibling is back in a free cluster and until would seed it.
    """
    from datetime import timedelta

    from app.models import Agent
    from app.services import fleet as fleet_svc

    _no_code_hits(monkeypatch)
    review_item = _item(db, "In Review", touchpoints=["backend/app/spanner.py"])
    sibling = _item(db, "Sibling", touchpoints=["backend/app/spanner.py"])
    items_svc.update_item(db, review_item.id, status="review")

    agent = Agent(id="CORE-A886", project_id="core", number=1886, label="886",
                  active_role="worker")
    db.add(agent)
    db.commit()
    fleet_svc.reserve_areas(
        db, agent_id=agent.id, item_id=review_item.id,
        areas=["backend/app/spanner.py"],
        expires_at=items_svc.utcnow() + timedelta(seconds=600),
        predicted=False,
    )
    db.commit()
    fleet_svc.release_reservations(db, item_id=review_item.id)

    clusters = collision.clusters_for_project(db, "core")
    seedable = [it for cl in clusters if not cl.get("held_by") for it in cl["items"]]
    members = [it for cl in clusters for it in cl["items"]]
    assert sibling.id not in seedable
    assert sibling.id not in members, "a stripped sibling must not be claimable"
    assert review_item.id not in seedable, "the review id itself is not a seed"
    assert seedable, "work outside the review glob stays startable"
    review_cluster = next(cl for cl in clusters if review_item.id in cl["items"])
    assert "review" in (review_cluster.get("held_by") or [])


def test_prediction_survives_an_embedding_failure(db, monkeypatch):
    """`propose_allocation` predicts areas for every ready item without touchpoints. On
    2026-09-23 one such item's text overflowed the embedder, the provider raised out of
    `search_code`, and every planner call was a 500 for as long as the item stayed open — no
    spawn possible. Inference is one of two signals; losing it is logged, not fatal."""
    import httpx

    def boom(db_, q, pid, top_k=5):
        raise httpx.HTTPStatusError("500 input too large", request=None, response=None)

    monkeypatch.setattr(code_graph, "search_code", boom)
    it = _item(db, "No touchpoints, long text", desc="context " * 1200)
    areas, src = collision.touch_areas(db, it, "core")
    assert src == "predicted" and areas == []
    clusters = collision.collision_clusters(db, [it], "core")
    assert [c["items"] for c in clusters] == [[it.id]]
