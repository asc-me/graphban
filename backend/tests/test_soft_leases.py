"""Soft leases (GRPH-1389): overlapping areas git can merge no longer block a second drain.

`areas_collide` still defines clustering. Reservations are exclusive only where git cannot
see the collision: `backend/alembic/versions/*`, where two new migrations both merge cleanly
and leave two heads.
"""
from datetime import timedelta

import pytest

from app.services import collision as collision_svc
from app.services import fleet as fleet_svc
from app.services import items as items_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, touchpoints):
    r = client.post("/api/items", json={"title": title, "project_id": "core",
                                        "touchpoints": touchpoints}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _agent(db, n):
    from app.models import Agent

    a = Agent(id=f"CORE-A{n}", project_id="core", number=n, label=f"drain-{n}",
              active_role="worker")
    db.add(a)
    db.commit()
    return a.id


def _hold(db, agent, areas, item_id):
    fleet_svc.reserve_areas(db, agent_id=agent, item_id=item_id, areas=areas,
                            expires_at=items_svc.utcnow() + timedelta(seconds=600),
                            predicted=False)
    db.commit()


def test_hard_area_classification():
    assert fleet_svc.is_hard_area("backend/alembic/versions/*")
    assert fleet_svc.is_hard_area("backend/alembic/versions/0188_x.py")
    assert not fleet_svc.is_hard_area("backend/app/services/fleet.py")
    assert not fleet_svc.is_hard_area("backend/alembic/env.py")


def test_soft_overlap_does_not_collide_for_reservations():
    a, b = ["backend/app/services/fleet.py"], ["backend/app/services"]
    assert fleet_svc.areas_collide(a, b), "clustering still sees the overlap"
    assert fleet_svc.hard_collide(a, b) == []


def test_two_migrations_still_collide():
    a = ["backend/alembic/versions/0188_a.py"]
    b = ["backend/alembic/versions/*"]
    assert fleet_svc.hard_collide(a, b)
    assert fleet_svc.hard_collide(b, a)


def test_second_drain_claims_over_a_soft_overlap_without_waiting(client, auth, db):
    held = _item(client, auth, "first", ["backend/app/services/fleet.py"])
    second = _item(client, auth, "second", ["backend/app/services/fleet.py"])
    holder, other = _agent(db, 71), _agent(db, 72)
    _hold(db, holder, ["backend/app/services/fleet.py"], held)

    got = fleet_svc.claim_cluster(db, agent_id=other, project_id="core")

    assert got["claimed"] is True, got
    assert second in {i["stored_id"] for i in got["items"]}, got


def test_second_drain_is_held_by_a_migration_lease(client, auth, db):
    held = _item(client, auth, "first", ["backend/alembic/versions/*"])
    second = _item(client, auth, "second", ["backend/alembic/versions/*"])
    holder, other = _agent(db, 73), _agent(db, 74)
    _hold(db, holder, ["backend/alembic/versions/*"], held)

    got = fleet_svc.claim_cluster(db, agent_id=other, project_id="core")

    # Other free clusters may be claimed; the migration cluster must not be.
    assert second not in {i["stored_id"] for i in got.get("items") or []}, got


def test_a_cluster_over_a_soft_overlap_carries_no_holder(client, auth, db):
    held = _item(client, auth, "first", ["backend/app/x.py"])
    holder = _agent(db, 75)
    _hold(db, holder, ["backend/app/x.py"], held)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "backend/app/x.py" in (c.get("areas") or []))

    assert "held_by" not in mine
    # … but it says what it overlaps, so the drain can report it (GRPH-1389).
    assert mine["soft_overlap"] == [{"area": "backend/app/x.py", "reserved": "backend/app/x.py",
                                     "by": holder}]


def test_a_cluster_over_a_migration_lease_names_its_holder(client, auth, db):
    held = _item(client, auth, "first", ["backend/alembic/versions/*"])
    holder = _agent(db, 76)
    _hold(db, holder, ["backend/alembic/versions/*"], held)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "backend/alembic/versions/*" in (c.get("areas") or []))

    assert mine["held_by"] == [holder]
    assert "soft_overlap" not in mine


# ---- a signed-off item whose merge hits a rebase conflict can go back (GRPH-1389) ---------------

def _done(db, client, auth, *, evidence=None):
    from app.models import Item

    item_id = _item(client, auth, "signed off, PR behind its base", ["backend/app/x.py"])
    row = db.get(Item, item_id)
    row.status = "done"
    row.built_by = "CORE-A71"
    row.reviewed_by = "CORE-A72"
    row.evidence = evidence or []
    db.commit()
    return item_id


def test_a_done_item_whose_merge_has_not_landed_can_be_bounced(client, auth, db):
    """The Merger only watches `done` items. A rebase conflict there must be able to send the
    item back. Sabotage: make `_require_bounceable` call `_require_in_review` for `done` too
    and this fails with NotInReview."""
    item_id = _done(db, client, auth)

    item = fleet_svc.bounce(db, item_id=item_id, agent_id="CORE-A73",
                            reason="rebase conflict in backend/app/x.py")

    assert item.status == "next"
    assert item.bounce_reason == "rebase conflict in backend/app/x.py"
    assert item.bounce_pinned_to == "CORE-A71", "the author keeps the worktree"
    assert item.reviewed_by is None


def test_a_done_item_whose_merge_landed_is_not_bounced(client, auth, db):
    item_id = _done(db, client, auth, evidence=[{
        "kind": "url", "detail": "merged by gbfleet: squash of #9 landed as abcdef123456",
        "url": "https://github.com/o/r/pull/9", "commit": "abcdef1234567890"}])

    with pytest.raises(fleet_svc.NotInReview, match="merge has landed"):
        fleet_svc.bounce(db, item_id=item_id, agent_id="CORE-A73", reason="too late")


def test_work_in_progress_is_still_not_bounceable(client, auth, db):
    from app.models import Item

    item_id = _item(client, auth, "still building", ["backend/app/y.py"])
    db.get(Item, item_id).status = "in_progress"
    db.commit()

    with pytest.raises(fleet_svc.NotInReview, match="not awaiting review"):
        fleet_svc.bounce(db, item_id=item_id, agent_id="CORE-A73", reason="no")
