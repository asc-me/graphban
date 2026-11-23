"""GRPH-1873: a custom rule is evaluated where its primitive runs (P91 Goal 1, D5, D6).

The rule is proposed by a real agent (propose_rule), adopted by a person, then promoted, with
the library `plan.exclusive_areas` loaded — both active and observing — as on a real box."""
import pytest

from app.db import SessionLocal
from app.errors import Validation
from app.models import Agent, Item, RuleObservation, RuleToggle, utcnow
from app.services import code_graph
from app.services import custom_rules as C
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_rule_proposals import _call, env  # noqa: F401

RID = "custom.p91-walk-area"
P = "docs/p91-walk"
PROPOSER = "GRPH-A9001"
LIB_PATHS = ["db/migrations"]  # the library rule's own paths never touch P


@pytest.fixture(params=["library_active", "library_observing"])
def box(request, client, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    monkeypatch.setattr(T, "LIBRARY", dict(T.LIBRARY))
    T.register(T.LibraryRule(
        id=T.EXCLUSIVE_AREAS_RULE, stage="plan", title="Exclusive file areas", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"paths": LIB_PATHS}))
    s = SessionLocal()
    s.add(Agent(id=PROPOSER, project_id=env["pid"], number=9001, label=PROPOSER,
                active_role="worker"))
    # the proposal's cited item is not part of the pair under test
    for it in s.query(Item).filter_by(project_id=env["pid"]):
        it.status, it.blocker = "blocked", "not under test"
    if request.param == "library_observing":
        s.add(RuleToggle(rule_id=T.EXCLUSIVE_AREAS_RULE, project_id=env["pid"], on=True,
                         observing_since=utcnow()))
    s.commit()
    s.pid = env["pid"]
    s.env = env
    s.client = client
    assert (T.observing_since(s, T.EXCLUSIVE_AREAS_RULE, s.pid) is not None) == \
        (request.param == "library_observing")
    try:
        yield s
    finally:
        s.close()


def _not_used(db) -> int:
    """The legend's NOT USED count: rules drawn `used: false` across the flow."""
    flow = db.client.get(f"/api/rules/flow?project_id={db.pid}", headers=db.env["h"])
    assert flow.status_code == 200, flow.text
    return sum(1 for s in flow.json()["stages"] for n in s["rules"] if not n["used"])


def _propose_and_adopt(db) -> int:
    """PROPOSER files the rule over MCP; a person adopts it (observing). Returns the
    not-used count read just before adoption."""
    r = _call(db.client, db.env["key"], "propose_rule", {
        "agent_id": PROPOSER, "kind": "new_rule", "citations": db.env["cites"],
        "draft": {"primitive": "exclusive_areas", "slug": "p91-walk-area", "title": "Walk area",
                  "why": "walk docs clash", "params": {"paths": [P + "/"]}}})
    assert not r.get("isError"), r
    prop = r["structuredContent"]
    assert prop["proposer"] == PROPOSER and prop["rule_id"] == RID
    before = _not_used(db)
    a = db.client.post(f"/api/projects/{db.pid}/rules/proposals/adopt", headers=db.env["h"],
                       json={"proposal_ids": [prop["id"]], "reason": "walk"})
    assert a.status_code == 200, a.text
    db.expire_all()
    assert C.get(db, RID, db.pid).state == "observing"
    return before


def _promote(db):
    r = db.client.post(f"/api/projects/{db.pid}/rules/{RID}/promote", headers=db.env["h"],
                       json={"reason": "it would have held"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert C.get(db, RID, db.pid).state == "active"


def _items(db):
    return [items_svc.create_item(db, title=t, project_id=db.pid, touchpoints=[P + f"/{t}.md"],
                                  status="next").id for t in ("one", "two")]


def _claim(db, agent):
    if db.get(Agent, agent) is None:
        db.add(Agent(id=agent, project_id=db.pid, number=db.query(Agent).count() + 1,
                     label=agent, active_role="worker"))
        db.commit()
    return fleet_svc.claim_cluster(db, agent_id=agent, project_id=db.pid, max_items=1)


def test_observing_custom_rule_records_would_hold_and_does_not_hold(box):
    db = box
    _propose_and_adopt(db)
    ids = _items(db)
    first = _claim(db, "GRPH-A1")
    assert first["claimed"]
    second = _claim(db, PROPOSER)
    assert second["claimed"], second
    rows = list(db.query(RuleObservation).filter_by(rule_id=RID, outcome="would_hold"))
    assert len(rows) == 1
    held_item = first["items"][0]["id"]
    assert rows[0].detail["held_by"] == "GRPH-A1" and rows[0].detail["held_item"] == held_item
    assert rows[0].item_id == second["items"][0]["id"] and set(ids) == {
        held_item, rows[0].item_id}
    # the library rule's own paths do not cover P: nothing recorded against it
    assert not db.query(RuleObservation).filter_by(rule_id=T.EXCLUSIVE_AREAS_RULE).count()


def test_promoted_custom_rule_holds_the_proposer_and_names_itself(box):
    db = box
    _propose_and_adopt(db)
    _promote(db)
    _items(db)
    assert _claim(db, "GRPH-A1")["claimed"]
    second = _claim(db, PROPOSER)  # D6: the proposing agent is held like any other
    assert not second["claimed"], second
    assert RID in second["reason"]


def test_observing_custom_rule_is_used_advisory_and_legend_unchanged(box):
    db = box
    before = _propose_and_adopt(db)
    assert _not_used(db) == before
    flow = db.client.get(f"/api/rules/flow?project_id={db.pid}", headers=db.env["h"]).json()
    node = next(n for s in flow["stages"] for n in s["rules"] if n["id"] == RID)
    assert node["used"] is True and node["enforcement"] == "advisory"


def test_library_rule_unchanged_alongside_a_custom_rule(box):
    """`plan.exclusive_areas` still holds its own paths when active, and nothing when observing."""
    db = box
    _propose_and_adopt(db)
    _promote(db)
    for t in ("m1", "m2"):
        items_svc.create_item(db, title=t, project_id=db.pid,
                              touchpoints=[f"db/migrations/{t}.py"], status="next")
    assert _claim(db, "GRPH-A1")["claimed"]
    second = _claim(db, "GRPH-A2")
    observing = T.observing_since(db, T.EXCLUSIVE_AREAS_RULE, db.pid) is not None
    assert second["claimed"] is observing, second
    if not observing:
        assert RID not in second["reason"]


def test_primitives_no_server_path_evaluates_are_refused_at_draft():
    with pytest.raises(Validation, match="generated_files"):
        C.refuse_unevaluated("generated_files")
    C.refuse_unevaluated("exclusive_areas")
