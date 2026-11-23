"""GRPH-1873: a custom rule is evaluated where its primitive runs (P91 Goal 1, D5, D6)."""
import pytest

from app.db import SessionLocal
from app.errors import Validation
from app.models import Agent, CustomRule, RuleObservation
from app.services import code_graph
from app.services import custom_rules as C
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import rules as R

RID = "custom.p91-walk-area"
P = "docs/p91-walk"


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=_login(client)).json()["id"]
    s = SessionLocal()
    s.pid = pid
    try:
        yield s
    finally:
        s.close()


def _rule(db, state):
    db.add(CustomRule(rule_id=RID, project_id=db.pid, primitive="exclusive_areas",
                      primitive_api=C.rule_pack.PRIMITIVE_API, stage="Plan",
                      params={"paths": [P + "/"]}, title="Walk area", why="w",
                      claims="enforced", state=state))
    db.commit()


def _item(db, title, tp):
    return items_svc.create_item(db, title=title, project_id=db.pid, touchpoints=[tp],
                                 status="next")


def _claim(db, agent):
    if db.get(Agent, agent) is None:
        db.add(Agent(id=agent, project_id=db.pid, number=db.query(Agent).count() + 1,
                     label=agent, active_role="worker"))
        db.commit()
    return fleet_svc.claim_cluster(db, agent_id=agent, project_id=db.pid, max_items=1)


def test_observing_custom_rule_records_would_hold_and_does_not_hold(db):
    _rule(db, "observing")
    _item(db, "one", P + "/a.md")
    _item(db, "two", P + "/b.md")
    assert _claim(db, "proposer")["claimed"]
    assert _claim(db, "other")["claimed"]
    rows = list(db.query(RuleObservation).filter_by(rule_id=RID, outcome="would_hold"))
    assert len(rows) == 1
    assert rows[0].detail["held_by"] == "proposer" and rows[0].detail["held_item"]
    assert rows[0].item_id


def test_promoted_custom_rule_holds_the_pair_and_names_itself_even_for_the_proposer(db):
    _rule(db, "active")
    _item(db, "one", P + "/a.md")
    _item(db, "two", P + "/b.md")
    assert _claim(db, "first")["claimed"]
    second = _claim(db, "proposer")  # D6: the proposer is held like any other agent
    assert not second["claimed"]
    assert RID in second["reason"]


def test_observing_custom_rule_is_used_advisory_and_not_counted_off(db):
    _rule(db, "observing")
    stages = {s["id"]: s for s in R.flow(db, db.pid)["stages"]}
    node = next(n for n in stages["plan"]["rules"] if n["id"] == RID)
    assert node["used"] is True and node["enforcement"] == "advisory"


def test_primitives_no_server_path_evaluates_are_refused_at_draft():
    with pytest.raises(Validation, match="generated_files"):
        C.refuse_unevaluated("generated_files")
    C.refuse_unevaluated("exclusive_areas")
