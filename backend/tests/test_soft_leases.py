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


def test_a_cluster_over_a_migration_lease_names_its_holder(client, auth, db):
    held = _item(client, auth, "first", ["backend/alembic/versions/*"])
    holder = _agent(db, 76)
    _hold(db, holder, ["backend/alembic/versions/*"], held)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "backend/alembic/versions/*" in (c.get("areas") or []))

    assert mine["held_by"] == [holder]
