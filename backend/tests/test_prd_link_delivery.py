"""GRPH-P104 S3: link requests and thread messages reach agents working the PRD as a directive."""
from app.models import Agent, Item
from app.services import fleet as fleet_svc
from app.services import threads as T
from app.services import links as L

from tests.test_prd_links import _a, _b, world  # noqa: F401


def _holder(db, prd, project, n):
    agent = Agent(id=f"{project}-A{n}", project_id=project, number=n, label=str(n))
    db.add(agent)
    db.add(Item(id=f"{project}-I{n}", project_id=project, number=n, title="work", status="in_progress",
                claimed_by=agent.id, prd_id=prd.id))
    db.commit()
    return agent.id


def test_message_is_a_directive_on_next_heartbeat_of_b_holder(world):
    db, a, b, _x = world
    holder = _holder(db, b, "pb", 1)
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="need contract")
    # The request itself is delivered first, once.
    d = fleet_svc.collect_directive(db, holder)
    assert d["type"] == "prd_link_pending" and d["pending"][0]["type"] == "prd_link_request"
    assert fleet_svc.collect_directive(db, holder) is None
    L.accept_prd_link(db, _b(), link.id)
    T.post(db, _a(), link.id, kind="question", body="which field?")
    d = fleet_svc.collect_directive(db, holder)
    assert d["pending"][0]["type"] == "prd_link_messages" and d["pending"][0]["count"] == 1
    assert fleet_svc.collect_directive(db, holder) is None


def test_own_side_message_and_non_workers_get_nothing_and_it_stays_pending(world):
    db, a, b, _x = world
    link = L.request_prd_link(db, _a(), a.id, b.id, reason="r")
    L.accept_prd_link(db, _b(), link.id)
    a_holder = _holder(db, a, "pa", 2)
    idle = Agent(id="pb-idle", project_id="pb", number=3, label="idle")
    db.add(idle)
    db.commit()
    T.post(db, _a(), link.id, kind="note", body="fyi")
    assert fleet_svc.collect_directive(db, a_holder) is None  # wrote it themselves
    assert fleet_svc.collect_directive(db, idle.id) is None
    # Nobody on B works it: still pending, visible to get_context's source.
    pend = T.pending_for_prds(db, [b.id])
    assert [p["type"] for p in pend] == ["prd_link_messages"]
    # Then a B agent claims an item and is told.
    late = _holder(db, b, "pb", 4)
    assert fleet_svc.collect_directive(db, late)["pending"][0]["count"] == 1
