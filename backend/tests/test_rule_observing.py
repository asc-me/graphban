"""GRPH-1777 (P91 S5): an observing rule records would-have outcomes at server-enforced
primitives instead of holding/refusing; a person promotes; the nudge never promotes itself."""
from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Agent, RuleObservation, RuleProposal, RuleToggle, utcnow
from app.services import code_graph
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import rule_observations as RO
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.EXCLUSIVE_AREAS_RULE
A = "db/migrations/0002_a.sql"
B = "db/migrations/0003_b.sql"
C = "db/migrations/0004_c.sql"


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="plan", title="Exclusive file areas", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"paths": ["db/migrations"]}))
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=_login(client)).json()["id"]
    s = SessionLocal()
    s.pid = pid
    try:
        yield s
    finally:
        s.close()


def _observe(db, since=None):
    db.add(RuleToggle(rule_id=RULE, project_id=db.pid, on=True,
                      observing_since=since or utcnow()))
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


def _obs(db):
    return list(db.query(RuleObservation).filter_by(rule_id=RULE))


def test_observing_does_not_hold_and_reports_would_have_held_then_promoted_holds(client, db):
    _observe(db)
    _item(db, "one", A)
    _item(db, "two", B)
    assert _claim(db, "agent-1")["claimed"]
    second = _claim(db, "agent-2")
    assert second["claimed"], second  # not held while observing
    state = T.resolve(db, RULE, db.pid)
    assert state["state"] == "observing"
    assert state["observation"]["would_hold"] == 1
    assert len(_obs(db)) == 1
    # a retried check is the same outcome, not a second one
    _item(db, "three", C)
    # promote: a person, with a reason
    h = _login(client)
    r = client.post(f"/api/projects/{db.pid}/rules/{RULE}/promote", json={"reason": "enough"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "active"
    db.expire_all()
    held = _claim(db, "agent-3")
    assert not held["claimed"], held  # same collision, now held
    assert len(_obs(db)) == 1  # history kept, nothing re-evaluated


def test_decision_due_at_ten_outcomes_and_never_self_promotes(db):
    _observe(db)
    for i in range(10):
        RO.record(db, db.pid, RULE, "would_refuse", item_id=f"i{i}")
    db.commit()
    s = T.resolve(db, RULE, db.pid)
    assert s["observation"]["decision_due"] and s["state"] == "observing"
    # a week later with nobody acting: still observing
    row = db.query(RuleToggle).filter_by(rule_id=RULE, project_id=db.pid).one()
    row.observing_since = utcnow() - timedelta(days=7)
    db.commit()
    for o in _obs(db):
        o.at = utcnow() - timedelta(days=6)
    db.commit()
    s = T.resolve(db, RULE, db.pid)
    assert s["state"] == "observing" and s["observation"]["decision_due"]


def test_decision_due_after_fourteen_days_only(db):
    _observe(db, since=utcnow() - timedelta(days=13))
    assert not T.resolve(db, RULE, db.pid)["observation"]["decision_due"]
    db.query(RuleToggle).update({"observing_since": utcnow() - timedelta(days=15)})
    db.commit()
    assert T.resolve(db, RULE, db.pid)["observation"]["decision_due"]


def test_agent_key_cannot_promote(client, db):
    _observe(db)
    h = _login(client)
    key = client.post("/api/api-keys", json={"name": "a", "project_id": db.pid},
                      headers=h).json()["plaintext"]
    r = client.post(f"/api/projects/{db.pid}/rules/{RULE}/promote", json={"reason": "x"},
                    headers={"X-API-Key": key})
    assert r.status_code == 403 and T.PERSON_RULE in r.text
    db.expire_all()
    assert T.resolve(db, RULE, db.pid)["state"] == "observing"


def test_evaluation_never_reads_the_proposer(db):
    """D6: the agent that proposed the rule is held/recorded like anyone else."""
    _observe(db)
    db.add(RuleProposal(project_id=db.pid, kind="turn_on", rule_id=RULE, primitive="exclusive_areas",
                        draft={}, params={}, citations=[], proposer="agent-proposer",
                        proposer_kind="agent", direction="tighten", status="adopted",
                        created_at=utcnow()))
    db.commit()
    _item(db, "one", A)
    _item(db, "two", B)
    _claim(db, "agent-other")
    _claim(db, "agent-proposer")
    assert [o.detail["held_by"] for o in _obs(db)] == ["agent-other"]


def test_promote_keeps_history_and_requires_observing(client, db):
    h = _login(client)
    RO.record(db, db.pid, RULE, "would_hold", item_id="x")
    db.commit()
    r = client.post(f"/api/projects/{db.pid}/rules/{RULE}/promote", json={"reason": "x"}, headers=h)
    assert r.status_code == 422  # not observing
    assert len(_obs(db)) == 1
