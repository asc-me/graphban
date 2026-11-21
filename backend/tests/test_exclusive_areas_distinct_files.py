"""GRPH-1742: two items under one exclusive prefix collide even when their files differ."""
import pytest

from app.db import SessionLocal
from app.services import code_graph, collision
from app.services import fleet as fleet_svc
from app.services import items as items_svc

MIG = ["db/migrations"]
A = "db/migrations/0002_a.sql"
B = "db/migrations/0003_b.sql"


@pytest.fixture()
def db(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, title, tps):
    return items_svc.create_item(db, title=title, project_id="core", touchpoints=tps,
                                 status="next")


def test_hard_collide_names_the_shared_prefix():
    assert fleet_svc.hard_collide([A], [B], MIG) == ["db/migrations"]
    assert fleet_svc.describe_pairs(fleet_svc.colliding_pairs([A], [B], hard=True,
                                                             hard_areas=MIG))


def test_only_one_under_the_prefix_does_not_collide():
    assert not fleet_svc.hard_collide(["db/seed.sql"], [A], MIG)


def test_default_alembic_prefix_serialises_without_a_project_value():
    a = "backend/alembic/versions/aaaa_one.py"
    b = "backend/alembic/versions/bbbb_two.py"
    assert fleet_svc.hard_collide([a], [b]) == ["backend/alembic/versions"]


def test_partition_joins_them_and_leaves_the_unrelated_alone():
    areas = {"x": [A], "y": [B], "z": ["db/seed.sql"]}
    pred = {k: False for k in areas}
    hards = fleet_svc._hard_paths(MIG)
    got = collision._partition(["x", "y", "z"], areas, pred, hards)
    assert sorted(sorted(c["items"]) for c in got) == [["x", "y"], ["z"]]
    # without hard areas the old behaviour holds
    assert len(collision._partition(["x", "y", "z"], areas, pred)) == 3


def test_preview_lists_both(db):
    _item(db, "one", [A, "tests/test_a.py"])
    _item(db, "two", [B, "tests/test_b.py"])
    _item(db, "seed", ["db/seed.sql"])
    out = collision.serialisation_preview(db, "core", MIG)
    titles = {i["title"] for i in out["serialised"]}
    assert titles == {"one", "two"}


def test_collision_clusters_put_them_in_one_cluster(db, monkeypatch):
    monkeypatch.setattr(fleet_svc, "project_hard_areas", lambda db, pid: tuple(MIG))
    one = _item(db, "one", [A])
    two = _item(db, "two", [B])
    got = collision.collision_clusters(db, [one, two], "core")
    assert len(got) == 1 and got[0]["collides"]


def test_review_hold_keeps_the_prefix_and_frees_the_unrelated():
    """A sibling under a review member's exclusive prefix stays held; a disjoint one is freed
    and re-partitioned (the path that raised NameError on the first attempt)."""
    class _It:
        def __init__(self, id, status):
            self.id, self.key, self.status, self.touchpoints = id, id.upper(), status, []

    pool = [_It("r", "review"), _It("s", "next"), _It("u", "next")]
    areas = {"r": [A, "x.py"], "s": [B], "u": ["x.py", "other.py"]}
    pred = {k: False for k in areas}
    hards = fleet_svc._hard_paths(MIG)
    clusters = collision._partition(["r", "s", "u"], areas, pred, hards)
    assert len(clusters) == 1
    out = collision._occupy_review_globs(clusters, pool, areas, pred, hards)
    held = [c for c in out if "r" in c["items"]][0]
    assert "S" in held["held_because"][0]["held"]
    assert not any("s" in c["items"] for c in out if c is not held)


def test_clusters_for_project_runs_with_a_review_member(db, monkeypatch):
    monkeypatch.setattr(fleet_svc, "project_hard_areas", lambda db, pid: tuple(MIG))
    one = _item(db, "one", [A])
    _item(db, "two", [B])
    _item(db, "seed", ["db/seed.sql"])
    items_svc.update_item(db, one.id, status="review")
    got = collision.clusters_for_project(db, "core")
    assert got is not None
