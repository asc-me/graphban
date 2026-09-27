"""Explicit disjoint touchpoints never cluster by directory; holds apply per file (GRPH-951).

SA-574..583 each declared their own files — `NeedsYou.tsx` + `styles/needs.css`,
`CueQueue.tsx` + `styles/queue.css` — and the dry run still showed one cluster, "only because
their files share a directory (GRPH-810)". Nine independent screens were built one at a time,
and SA-576 in review, held by a reviewer that had exited, idled the next wave.

Sabotage: restore the unconditional directory branch in `clustering.why_match`. The
disjoint-files tests below then see one cluster and fail. `areas_collide`'s prefix rule must
not bring the merge back through the reservation path, which is its own test.
"""
from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Agent
from app.services import clustering, code_graph, collision
from app.services import fleet as fleet_svc
from app.services import items as items_svc


@pytest.fixture()
def db(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, title, touchpoints):
    return items_svc.create_item(db, title=title, description="", project_id="core",
                                 touchpoints=touchpoints)


def _with(clusters, item_id):
    return next(c for c in clusters if item_id in c["items"])


def test_two_items_naming_disjoint_files_in_one_directory_are_two_clusters(db):
    """THE ONE THAT MATTERS — the SA-P21 shape, measured through the partition."""
    needs = _item(db, "Needs you", ["web/src/NeedsYou.tsx", "web/src/styles/needs.css"])
    queue = _item(db, "Cue queue", ["web/src/CueQueue.tsx", "web/src/styles/queue.css"])

    clusters = collision.clusters_for_project(db, "core")

    assert _with(clusters, needs.id)["items"] == [needs.id]
    assert _with(clusters, queue.id)["items"] == [queue.id]


def test_nine_screens_are_nine_clusters(db):
    ids = [_item(db, f"screen {n}", [f"web/src/Screen{n}.tsx", f"web/src/styles/s{n}.css"]).id
           for n in range(9)]

    clusters = collision.clusters_for_project(db, "core")

    assert sorted(len(c["items"]) for c in clusters if set(c["items"]) & set(ids)) == [1] * 9


def test_a_directory_or_a_glob_still_joins_and_names_its_rule(db):
    """The rule stays for the vague side, and the reason says which rule it was."""
    dir_side = _item(db, "a directory", ["web/src/features"])
    file_side = _item(db, "a file", ["web/src/App.tsx"])
    glob_side = _item(db, "a glob", ["lib/*.ts"])
    under = _item(db, "under the glob", ["lib/api.ts"])

    clusters = collision.clusters_for_project(db, "core")

    joined = _with(clusters, dir_side.id)
    assert file_side.id in joined["items"]
    assert {r["rule"] for m in joined["because"] for r in m["on"]} == {"directory"}
    globbed = _with(clusters, glob_side.id)
    assert under.id in globbed["items"]
    assert {r["rule"] for m in globbed["because"] for r in m["on"]} == {"glob"}


def test_why_match_directory_needs_a_vague_side():
    assert clustering.why_match("a/x.py", "a/y.py") == ""
    assert clustering.why_match("a/x.py", "a/sub") == "directory"
    assert clustering.why_match("a/Makefile", "a/y.py") == "directory"
    assert clustering.why_match("a/x.py", "a/x.py") == "exact"


def test_the_reservation_rule_does_not_bring_the_merge_back():
    """`areas_collide` unions the partition's rule with a prefix rule. Two sibling files are
    not prefixes of each other, so a reservation on one must not hold the other."""
    assert fleet_svc.areas_collide(["web/src/NeedsYou.tsx"], ["web/src/CueQueue.tsx"]) == []
    assert fleet_svc.areas_collide(["web/src/NeedsYou.tsx"], ["web/src"]) == ["web/src/NeedsYou.tsx"]


def test_a_reservation_on_one_file_leaves_its_neighbour_claimable(db):
    """CALL. The divvy's hold path, not the matcher: a live reservation on CueQueue.tsx
    must not mark NeedsYou.tsx's cluster held."""
    needs = _item(db, "Needs you", ["web/src/NeedsYou.tsx"])
    queue = _item(db, "Cue queue", ["web/src/CueQueue.tsx"])
    agent = Agent(id="CORE-A951", project_id="core", number=1951, label="951",
                  active_role="worker")
    db.add(agent)
    db.commit()
    items_svc.claim_item(db, queue.id, agent.id)
    fleet_svc.reserve_areas(db, agent_id=agent.id, item_id=queue.id,
                            areas=["web/src/CueQueue.tsx"],
                            expires_at=items_svc.utcnow() + timedelta(seconds=600),
                            predicted=False)
    db.commit()

    got = _with(collision.clusters_for_project(db, "core"), needs.id)

    assert not got.get("held_by"), got.get("held_because")


def _review_cluster(db):
    """R in review. B overlaps R (a glob over R's file) and also names lib/x.ts; C names only
    lib/x.ts. One cluster by transitivity, and C shares nothing with R."""
    r = _item(db, "SA-576 in review", ["ui/CueQueue.tsx", "ui/styles/queue.css"])
    b = _item(db, "overlaps review", ["ui/*.tsx", "lib/x.ts"])
    c = _item(db, "disjoint from review", ["lib/x.ts"])
    before = _with(collision.clusters_for_project(db, "core"), r.id)
    assert {r.id, b.id, c.id} <= set(before["items"]), "the fixture must start as one cluster"
    items_svc.update_item(db, r.id, status="review")
    return r, b, c


def test_a_review_member_holds_only_its_own_files(db):
    r, b, c = _review_cluster(db)

    clusters = collision.clusters_for_project(db, "core")
    seedable = [i for cl in clusters if not cl.get("held_by") for i in cl["items"]]
    members = [i for cl in clusters for i in cl["items"]]

    assert c.id in seedable, "a sibling disjoint from the review member stays claimable"
    assert b.id not in members, "a sibling that overlaps the review member is held"
    assert r.id not in seedable
    held = _with(clusters, r.id)
    assert held["areas"] == ["ui/CueQueue.tsx", "ui/styles/queue.css"]
    reason = next(h for h in held["held_because"] if h["reserved"] == "review")
    assert reason["holds"] == ["ui/CueQueue.tsx", "ui/styles/queue.css"]
    assert reason["rule"] == f"{r.key} is in review and holds ui/CueQueue.tsx, ui/styles/queue.css"
    assert reason["held"] == [b.key]


def test_claim_cluster_takes_the_freed_sibling(db):
    """CALL. Freed in the partition is not enough if the claim path still refuses it."""
    r, b, c = _review_cluster(db)
    agent = Agent(id="CORE-A952", project_id="core", number=1952, label="952",
                  active_role="worker")
    db.add(agent)
    db.commit()

    # Other tests' items share the project, so drain it rather than expect C first.
    claimed: list[str] = []
    for _ in range(50):
        got = fleet_svc.claim_cluster(db, agent_id=agent.id, project_id="core", max_items=10)
        if not got["claimed"]:
            break
        claimed += [i["stored_id"] for i in got["items"]]

    assert c.id in claimed, "the freed sibling was never handed out"
    assert b.id not in claimed and r.id not in claimed


def test_fleet_status_names_the_files_a_review_member_holds(db):
    r = _item(db, "in review", ["web/src/CueQueue.tsx", "web/src/styles/queue.css"])
    items_svc.update_item(db, r.id, status="review")

    got = [h for h in fleet_svc.fleet_status(db, "core")["review_holds"] if h["id"] == r.key]

    assert got == [{"id": r.key, "holds": ["web/src/CueQueue.tsx", "web/src/styles/queue.css"],
                    "detail": f"{r.key} holds web/src/CueQueue.tsx, web/src/styles/queue.css"}]
