"""Persisted collision decisions (PRD-60 D8 / GRPH-1138)."""
from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Agent, Link
from app.services import code_graph, collision
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


def _item(db, title, tp):
    return items_svc.create_item(db, title=title, description="", project_id="core",
                                 touchpoints=[tp])


def _agent(db, n):
    a = Agent(id=f"CORE-A{n}", project_id="core", number=n, label=f"w{n}", active_role="worker")
    db.add(a)
    db.commit()
    return a.id


def _trio(db, tp="src/zone"):
    return [_item(db, t, f"{tp}/*.py") for t in ("one", "two", "three")]


def _quiet(db, keep):
    """The fixture project carries seeded work; park it so only `keep` is in play."""
    from app.models import Item

    for it in db.query(Item).filter(Item.project_id == "core").all():
        if it.id not in {k.id for k in keep}:
            it.status = "done"
    db.commit()


def _members(items):
    return [i.id for i in items]


def _links(db):
    return db.query(Link).filter(Link.reason.like("collision%")).all()


def test_serialize_hands_members_out_in_order(db):
    a, b, c = _trio(db)
    _quiet(db, [a, b, c])
    collision.decide(db, "core", _members([a, b, c]), "serialize", authority="owner")
    got = []
    for n in (1, 2, 3):
        agent = _agent(db, n)
        it = items_svc.claim_next(db, agent, project_id="core")
        assert it is not None
        got.append(it.id)
        # Nothing else is ready until the leader is done.
        assert items_svc.claim_next(db, _agent(db, 10 + n), project_id="core") is None
        it.status = "done"
        db.commit()
    assert got == [a.id, b.id, c.id]


def test_override_refused_for_worker_allowed_for_planner_and_scoped(db):
    a, b, c = _trio(db)
    d = _item(db, "other", "web/zone/*.tsx")
    e = _item(db, "other2", "web/zone/*.tsx")
    _quiet(db, [a, b, c, d, e])
    holder = _agent(db, 1)
    taker = _agent(db, 2)
    with pytest.raises(collision.OverrideRefused):
        collision.decide(db, "core", _members([a, b, c]), "override", authority="worker")
    with pytest.raises(collision.OverrideRefused):
        collision.decide(db, "core", _members([a, b, c]), "override", authority="writer")

    now = items_svc.utcnow()
    for x in (a, d):
        items_svc.claim_item(db, x.id, holder)
        fleet_svc.reserve_areas(db, agent_id=holder, item_id=x.id, areas=list(x.touchpoints),
                                expires_at=now + timedelta(seconds=600), predicted=False)
    db.commit()
    # Both clusters are held by `holder`: refused.
    assert fleet_svc.claim_cluster(db, agent_id=taker, project_id="core")["claimed"] is False

    collision.decide(db, "core", _members([a, b, c]), "override", authority="planner")
    got = fleet_svc.claim_cluster(db, agent_id=taker, project_id="core")
    assert got["claimed"] is True
    claimed = {i["stored_id"] for i in got["items"]}
    assert claimed <= {b.id, c.id}
    # The other cluster is still refused.
    assert fleet_svc.claim_cluster(db, agent_id=_agent(db, 3), project_id="core")["claimed"] is False
    assert e.id  # untouched


def test_decision_expires_when_member_set_changes(db):
    a, b = _item(db, "a", "src/m/*.py"), _item(db, "b", "src/m/*.py")
    row = collision.decide(db, "core", _members([a, b]), "parallel", authority="owner")
    assert collision.triage(db, "core") == []
    _item(db, "late joiner", "src/m/*.py")
    assert [r.id for r in collision.decisions(db, "core")] == []
    feed = collision.triage(db, "core")
    assert len(feed) == 1 and len(feed[0]["items"]) == 3
    db.refresh(row)
    assert row.expired_at is not None


def test_events_and_undo_removes_exactly_its_links(db):
    from app.models import Event
    from app.services import links as links_svc

    a, b, c = _trio(db, "src/undo")
    # An authored dependency between two members must survive the undo.
    links_svc.create_link(db, a=c.id, b=b.id, type_="dependency", reason="authored",
                          project_id="core")
    row = collision.decide(db, "core", _members([a, b, c]), "serialize", authority="owner",
                           order=[a.id, b.id, c.id], actor="u1")
    assert {(lk.a, lk.b) for lk in _links(db)} == {(b.id, a.id)}
    collision.undo(db, row.id, actor="u1")
    assert _links(db) == []
    assert db.query(Link).filter(Link.reason == "authored").count() == 1
    actions = [e.action for e in db.query(Event).order_by(Event.id).all()
               if e.action.startswith("collision_")]
    assert actions == ["collision_decide", "collision_undo"]
    assert len(collision.triage(db, "core")) == 1  # back in triage


def test_hold_blocks_followers_and_snooze_hides_until_lapsed(db):
    a, b = _item(db, "a", "src/h/*.py"), _item(db, "b", "src/h/*.py")
    collision.decide(db, "core", _members([a, b]), "hold", authority="owner", order=[b.id, a.id])
    assert {(lk.a, lk.b) for lk in _links(db)} == {(a.id, b.id)}
    row = collision.decide(db, "core", _members([a, b]), "snooze", authority="owner",
                           snooze_until=items_svc.utcnow() + timedelta(hours=1))
    assert collision.triage(db, "core") == []
    row.snooze_until = items_svc.utcnow() - timedelta(minutes=1)
    db.commit()
    assert len(collision.triage(db, "core")) == 1
