"""A blocker that names a dependency clears when that dependency is done (GRPH-1530).

A free-text blocker used to gate readiness forever, including after the item it
named reached done. The drain then sat idle. A person or a decision is not that
case, and a claim does not hand out an item whose dependency is still open.
"""
import ast
import inspect
from datetime import timedelta

from app.db import SessionLocal
from app.models import Agent, Link, utcnow
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from tests import attest


def _project(client, auth, name: str) -> str:
    created = client.post("/api/projects", json={"name": name}, headers=auth)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _item(db, project, title, **fields):
    return items_svc.create_item(db, title=title, project_id=project, **fields)


def test_a_blocker_clears_when_its_named_dependency_is_done(client, auth):
    """Dropping release_when_named_blockers_done leaves this item blocked."""
    project = _project(client, auth, "Stall 1530")
    with SessionLocal() as db:
        dep = _item(db, project, "device code")
        waiting = _item(db, project, "uses the token", status="blocked")
        items_svc.update_item(db, waiting.id, blocker=f"needs {dep.key}")
        db.refresh(waiting)
        assert dep.key in (waiting.blocker or "")
        link = db.query(Link).filter(
            Link.a == waiting.id, Link.b == dep.id, Link.type == "dependency").one()
        assert link.reason == items_svc.BLOCKER_DEP_REASON

        attest.complete(db, dep.id)
        db.refresh(waiting)
        assert waiting.status == "next"
        assert waiting.blocker == ""
        assert any(
            (e.get("detail") or "").startswith(f"blocker cleared: {dep.key} done")
            for e in (waiting.evidence or [])
        )


def test_a_blocker_stays_until_every_named_item_is_done(client, auth):
    project = _project(client, auth, "Stall 1530 two")
    with SessionLocal() as db:
        first = _item(db, project, "first")
        second = _item(db, project, "second")
        waiting = _item(db, project, "waits on both", status="next")
        items_svc.update_item(
            db, waiting.id, blocker=f"needs {first.key} and {second.key}")
        attest.complete(db, first.id)
        db.refresh(waiting)
        assert waiting.blocker
        assert first.key in waiting.blocker
        attest.complete(db, second.id)
        db.refresh(waiting)
        assert waiting.status == "next"
        assert waiting.blocker == ""


def test_a_person_or_a_decision_is_not_auto_cleared(client, auth):
    project = _project(client, auth, "Stall 1530 human")
    with SessionLocal() as db:
        person_dep = _item(db, project, "unrelated")
        person = _item(db, project, "asks a person", status="blocked")
        items_svc.update_item(
            db, person.id, blocker=f"needs a person to look at {person_dep.key}")
        from app.services import links as links_svc
        links_svc.create_link(
            db, a=person.id, b=person_dep.id, type_="dependency",
            project_id=project, reason="see also")

        decision_dep = _item(db, project, "the option")
        decision = _item(db, project, "asks a decision", status="blocked",
                         tags=["wait:decision"])
        items_svc.update_item(db, decision.id, blocker=f"needs {decision_dep.key}")

        attest.complete(db, person_dep.id)
        attest.complete(db, decision_dep.id)
        db.refresh(person)
        db.refresh(decision)
        assert person.status == "blocked"
        assert person.blocker == f"needs a person to look at {person_dep.key}"
        assert decision.status == "blocked"
        assert decision_dep.key in (decision.blocker or "")


def test_claim_refuses_an_item_while_its_dependency_is_open(client, auth):
    """claim_next filters the sweep. A stale in_progress row stays in the cluster
    pool, so claim_item — the write claim_cluster uses — refuses that reclaim."""
    project = _project(client, auth, "Stall 1530 claim")
    with SessionLocal() as db:
        dep = _item(db, project, "still open", status="backlog")
        waiting = _item(db, project, "not yet", status="next")
        from app.services import links as links_svc
        links_svc.create_link(
            db, a=waiting.id, b=dep.id, type_="dependency", project_id=project)

        claimed = items_svc.claim_next(db, "stall-agent", project_id=project)
        assert claimed is not None and claimed.id == dep.id
        # A second sweep still does not hand the dependent out.
        other = items_svc.claim_next(db, "stall-agent-2", project_id=project)
        assert other is None or other.id != waiting.id

        # A stale in_progress row is in the cluster pool even when its dependency
        # is open. claim_item has to refuse it or claim_cluster hands it out.
        held = _item(db, project, "held open", status="blocked")
        items_svc.update_item(db, held.id, blocker="needs a person")
        stale = _item(db, project, "abandoned mid-build", status="in_progress")
        stale.claimed_by = "gone"
        stale.claimed_at = utcnow() - timedelta(seconds=10_000)
        db.commit()
        links_svc.create_link(
            db, a=stale.id, b=held.id, type_="dependency", project_id=project)
        db.add(Agent(
            id="stall-cluster", project_id=project, number=1, label="w",
            active_role="worker"))
        db.commit()

        assert items_svc.claim_item(db, stale.id, "stall-cluster") is None
        # The backlog dep above is already claimed. Nothing else is ready: the
        # stale row's dependency is still blocked.
        again = items_svc.claim_next(db, "stall-other", project_id=project)
        assert again is None or again.id != stale.id
        got = fleet_svc.claim_cluster(db, agent_id="stall-cluster", project_id=project)
        claimed_ids = {row["stored_id"] for row in got["items"]}
        assert stale.id not in claimed_ids
        assert got["claimed"] is False


def test_next_cluster_skips_a_neighbour_with_an_open_dependency(client, auth):
    """next_cluster claims neighbours through claim_item. A fresh row is not a
    stale reclaim, so dropping the skip here hands the neighbour out."""
    project = _project(client, auth, "Stall 1530 neighbour")
    with SessionLocal() as db:
        dep = _item(db, project, "still open", status="backlog")
        seed = _item(
            db, project, "the seed", status="next",
            touchpoints=["backend/app/services/items.py"])
        neighbour = _item(
            db, project, "rides along", status="next",
            touchpoints=["backend/app/services/items.py"])
        from app.services import clustering as cluster_svc
        from app.services import links as links_svc
        links_svc.create_link(
            db, a=neighbour.id, b=dep.id, type_="dependency", project_id=project)
        batch = cluster_svc.next_cluster(
            db, "stall-neighbour", project_id=project, max_items=3)
        ids = {row["item"].id for row in batch}
        assert seed.id in ids
        assert neighbour.id not in ids


def test_sign_off_calls_the_same_release():
    """sign_off writes done itself. Dropping this call leaves the blocker set."""
    tree = ast.parse(inspect.getsource(fleet_svc.sign_off))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name == "release_when_named_blockers_done":
            found = True
    assert found


def _dep(db, a, b):
    return db.query(Link).filter(
        Link.a == a.id, Link.b == b.id, Link.type == "dependency").one_or_none()


def test_clearing_a_blocker_drops_its_links_but_keeps_hand_made_ones(client, auth):
    project = _project(client, auth, "Stale 1867 clear")
    with SessionLocal() as db:
        x = _item(db, project, "x")
        y = _item(db, project, "y")
        a = _item(db, project, "a", status="next")
        db.add(Link(project_id=project, a=a.id, b=y.id, type="dependency",
                    reason="manual", confidence=1.0))
        db.commit()
        items_svc.update_item(db, a.id, blocker=f"needs {x.key} and {y.key}")
        assert _dep(db, a, x).reason == items_svc.BLOCKER_DEP_REASON
        items_svc.update_item(db, a.id, blocker="")
        assert _dep(db, a, x) is None
        assert _dep(db, a, y).reason == "manual"


def test_replacing_a_blocker_swaps_the_links(client, auth):
    project = _project(client, auth, "Stale 1867 swap")
    with SessionLocal() as db:
        x = _item(db, project, "x")
        y = _item(db, project, "y")
        a = _item(db, project, "a", status="next")
        items_svc.update_item(db, a.id, blocker=f"needs {x.key}")
        items_svc.update_item(db, a.id, blocker=f"needs {y.key}")
        assert _dep(db, a, x) is None
        assert _dep(db, a, y) is not None


def test_a_blocker_that_would_close_a_cycle_adds_no_link(client, auth):
    project = _project(client, auth, "Stale 1867 cycle")
    with SessionLocal() as db:
        a = _item(db, project, "a", status="next")
        b = _item(db, project, "b")
        db.add(Link(project_id=project, a=b.id, b=a.id, type="dependency",
                    reason="manual", confidence=1.0))
        db.commit()
        items_svc.update_item(db, a.id, blocker=f"waiting on {b.key}'s rework")
        db.refresh(a)
        assert b.key in a.blocker
        assert _dep(db, a, b) is None
