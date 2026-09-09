"""A cluster whose AREAS are reserved says so (GRPH-803).

Reported from super-arc: wave 2 spawned all ten children into fully-reserved clusters. Each
registered, found nothing, exited. The wave ended `{"ok": false, "reason": "cap", "spawned":
10, "minted": 0}` — it failed purely by arriving early, and burned its whole child budget
doing it.

An item's LEASE and its AREAS are different holds. `claimable` excludes an item somebody has
claimed; it says nothing about an item nobody holds whose files are reserved by an agent
working something else. So the pool read as free and every claim was refused on arrival.

`_delegate_next` in the supervisor has always skipped clusters carrying `held_by`. Nothing
ever set it — the guard has never once fired.
"""
import pytest

from app.services import collision as collision_svc
from app.services import fleet as fleet_svc


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


def _agent(db) -> str:
    """A real row: `area_reservations` has foreign keys on both agent and item, so a fake id
    fails the insert rather than the assertion."""
    from app.models import Agent

    existing = db.query(Agent).filter(Agent.project_id == "core").first()
    if existing:
        return existing.id
    agent = Agent(id="CORE-A99", project_id="core", number=99, label="holder",
                  active_role="worker")
    db.add(agent)
    db.commit()
    return agent.id


def _reserve(db, areas, item_id, *, seconds=600):
    from datetime import timedelta

    from app.services import items as items_svc

    fleet_svc.reserve_areas(db, agent_id=_agent(db), item_id=item_id, areas=areas,
                            expires_at=items_svc.utcnow() + timedelta(seconds=seconds),
                            predicted=False)
    db.commit()


def test_a_reserved_cluster_names_its_holder(client, auth, db):
    held = _item(client, auth, "held work", ["src/a.py"])
    _reserve(db, ["src/a.py"], held)

    got = collision_svc.clusters_for_project(db, "core")
    mine = [c for c in got if "src/a.py" in (c.get("areas") or [])]

    assert mine, "the item vanished from the pool entirely"
    assert mine[0]["held_by"] == [_agent(db)]


def test_it_says_when_the_earliest_frees(client, auth, db):
    """"Wait" and "give up" are different instructions, and the supervisor cannot tell them
    apart without this."""
    held = _item(client, auth, "held work", ["src/b.py"])
    _reserve(db, ["src/b.py"], held, seconds=300)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "src/b.py" in (c.get("areas") or []))

    assert 0 < mine["free_in"] <= 300


def test_an_unreserved_cluster_carries_no_holder(client, auth, db):
    """The control. A field set on everything would make the supervisor's skip-if-held check
    skip everything, which is a wave that never spawns."""
    _item(client, auth, "free work", ["src/c.py"])

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "src/c.py" in (c.get("areas") or []))

    assert "held_by" not in mine


def test_a_reservation_on_another_file_does_not_hold_this_cluster(client, auth, db):
    other = _item(client, auth, "other work", ["elsewhere/other.py"])
    _item(client, auth, "free work", ["src/d.py"])
    _reserve(db, ["elsewhere/other.py"], other)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "src/d.py" in (c.get("areas") or []))

    # A DIFFERENT DIRECTORY on purpose. The first version of this test used two paths under
    # src/ and they clustered together — `areas_collide` groups by parent directory, which is
    # GRPH-810 (reservations coarser than declared touchpoints) reproducing itself in a
    # fixture. Worth knowing: it is real, and it is not what this test is about.
    assert "held_by" not in mine


def test_the_item_is_still_offered_rather_than_hidden(client, auth, db):
    """MARKED, not filtered. A planner reading the divvy needs to see that work exists and is
    blocked — hiding it reads as 'nothing to do', which is the failure `claim_cluster`'s own
    refusal message was written to avoid."""
    held = _item(client, auth, "held work", ["src/e.py"])
    _reserve(db, ["src/e.py"], held)

    ids = {i for c in collision_svc.clusters_for_project(db, "core") for i in c["items"]}

    assert held in ids


def test_an_expired_reservation_holds_nothing(client, auth, db):
    stale = _item(client, auth, "free again", ["src/f.py"])
    _reserve(db, ["src/f.py"], stale, seconds=-60)

    got = collision_svc.clusters_for_project(db, "core")
    mine = next(c for c in got if "src/f.py" in (c.get("areas") or []))

    assert "held_by" not in mine
