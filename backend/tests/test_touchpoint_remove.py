"""GRPH-1398 — `update_item(touchpoints_remove=[...])` is the way off the touchpoint list."""
from datetime import timedelta

import pytest

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


def _agent(db, n=98, aid="CORE-A98"):
    from app.models import Agent

    a = db.get(Agent, aid)
    if a is None:
        a = Agent(id=aid, project_id="core", number=n, label="holder", active_role="worker")
        db.add(a)
        db.commit()
    return a.id


def _item(db, tps, **kw):
    it = items_svc.create_item(db, title="t", project_id="core", touchpoints=tps)
    for k, v in kw.items():
        setattr(it, k, v)
    db.commit()
    return it


def test_remove_drops_only_named_entries_and_records_it(db):
    it = _item(db, ["a.py", "backend/tests/*", "b.py"])
    items_svc.update_item(db, it.id, submitted_by="X", touchpoints_remove=["backend/tests/*"])
    db.refresh(it)
    assert it.touchpoints == ["a.py", "b.py"]
    assert any("backend/tests/*" in e.get("detail", "") for e in it.evidence)


def test_plain_touchpoints_still_union(db):
    it = _item(db, ["a.py"])
    items_svc.update_item(db, it.id, touchpoints=["b.py"])
    db.refresh(it)
    assert it.touchpoints == ["a.py", "b.py"]


def test_other_holder_refused_holder_and_planner_allowed(db):
    holder = _agent(db)
    it = _item(db, ["a.py", "b.py", "c.py"], status="in_progress", claimed_by=holder)
    with pytest.raises(ValueError, match="held by"):
        items_svc.update_item(db, it.id, submitted_by="CORE-A1", touchpoints_remove=["a.py"])
    items_svc.update_item(db, it.id, submitted_by="CORE-A1", touchpoints_remove=["a.py"],
                          remove_as_planner=True)
    items_svc.update_item(db, it.id, submitted_by=holder, touchpoints_remove=["b.py"])
    db.refresh(it)
    assert it.touchpoints == ["c.py"]


def test_reservation_on_removed_path_is_released(db):
    holder = _agent(db)
    it = _item(db, ["a.py", "wide/*"])
    fleet_svc.reserve_areas(db, agent_id=holder, item_id=it.id, areas=["a.py", "wide/*"],
                            expires_at=items_svc.utcnow() + timedelta(seconds=600),
                            predicted=False)
    db.commit()
    items_svc.update_item(db, it.id, submitted_by=holder, touchpoints_remove=["wide/*"])
    areas = {r.area for r in fleet_svc.active_reservations(db) if r.item_id == it.id}
    assert areas == {"a.py"}
    db.refresh(it)
    assert it.touchpoints == ["a.py"], "the reservation went but the touchpoint stayed"


def test_mcp_reply_lists_what_was_removed(client, auth):
    proj = client.post("/api/projects", json={"name": "Rm"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "rm", "project_id": proj},
                      headers=auth).json()["plaintext"]

    def call(tool, args):
        return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}},
                           headers={"X-API-Key": key}).json()["result"]["structuredContent"]

    item = call("create_item", {"title": "x", "touchpoints": ["a.py", "backend/tests/*"]})
    out = call("update_item", {"id": item["id"], "touchpoints_remove": ["backend/tests/*"]})
    assert out["touchpoints_removed"] == ["backend/tests/*"]
    assert out["touchpoints"] == ["a.py"]


def test_claim_that_collided_only_on_a_removed_path_now_succeeds(db):
    """Acceptance 3. The holder's `wide/*` refuses a second agent's claim on `wide/x.py`;
    once the holder removes `wide/*`, the same claim goes through. A no-op removal keeps
    both the touchpoint and its reservation, so the second claim is still refused."""
    from app.models import Agent

    holder = _agent(db)
    it = _item(db, ["a.py", "wide/*"], status="in_progress", claimed_by=holder)
    fleet_svc.reserve_areas(db, agent_id=holder, item_id=it.id, areas=["a.py", "wide/*"],
                            expires_at=items_svc.utcnow() + timedelta(seconds=600),
                            predicted=False)
    db.commit()
    target = _item(db, ["wide/x.py"], status="next")
    claimer = db.get(Agent, "CORE-A97") or Agent(id="CORE-A97", project_id="core", number=97,
                                                  label="claimer", active_role="worker")
    db.add(claimer)
    db.commit()

    def drain():
        # Other tests' items share the project: drain it rather than expect the target first.
        got_ids: list[str] = []
        for _ in range(50):
            got = fleet_svc.claim_cluster(db, agent_id=claimer.id, project_id="core",
                                          max_items=10)
            if not got["claimed"]:
                return got_ids, got
            got_ids += [i["stored_id"] for i in got["items"]]
        return got_ids, None

    before, refusal = drain()
    assert target.id not in before, "the target must start out refused on wide/*"
    assert refusal and "wide/*" in str(refusal), refusal

    items_svc.update_item(db, it.id, submitted_by=holder, touchpoints_remove=["wide/*"])

    after, _ = drain()
    assert target.id in after, "removing wide/* did not free the claim that collided on it"
