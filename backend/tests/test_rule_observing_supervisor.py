"""GRPH-1778 (P91 S6): observing for supervisor-read primitives. The server side of the
would-have post, and the UNMEASURED read for brief-read primitives."""
import pytest

from app.db import SessionLocal
from app.models import RuleToggle, utcnow
from app.services import code_graph
from app.services import delegation
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import rule_observations as RO
from app.services import rule_toggles as T
from app.services import rules as R


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    monkeypatch.setattr(T, "LIBRARY", {})
    for rid, params in ((T.GENERATED_FILES_RULE, {"paths": ["client/generated/"]}),
                        (T.UNDECLARED_FILES_RULE, {"mode": "hold"}),
                        (T.TEST_SCOPE_RULE, {"mode": "agent_full"}),
                        (T.PATH_CLASSES_RULE, {})):
        T.register(T.LibraryRule(
            id=rid, stage="build", title=rid, summary="s", enforcement="checked",
            enforcer=R.Enforcer("supervisor", "gbfleet until"),
            default_on=True, default_params=params))
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=_login(client)).json()["id"]
    s = SessionLocal()
    s.pid = pid
    try:
        yield s
    finally:
        s.close()


def _observe(db, rule_id):
    db.add(RuleToggle(rule_id=rule_id, project_id=db.pid, on=True, observing_since=utcnow()))
    db.commit()


def _item(db):
    return items_svc.create_item(db, title="x", project_id=db.pid, touchpoints=["a.py"],
                                 status="in_progress")


def test_observing_generated_files_records_would_allow(db):
    _observe(db, T.GENERATED_FILES_RULE)
    item = _item(db)
    out = fleet_svc._record_rule_would_have(
        db, item, rule_id=T.GENERATED_FILES_RULE, outcome="would_allow", commit="abc1234",
        branch="gb/x", reason="would have let through: client/generated/a.ts")
    db.commit()
    assert out["recorded"] is True
    rows = RO.history(db, db.pid, T.GENERATED_FILES_RULE)
    assert [(r.outcome, r.item_id, r.commit) for r in rows] == [("would_allow", item.id, "abc1234")]
    seen = T.resolve(db, T.GENERATED_FILES_RULE, db.pid)["observation"]
    assert seen["would_allow"] == 1 and seen["would_have"] == 1


def test_observing_undeclared_files_records_would_hold(db):
    _observe(db, T.UNDECLARED_FILES_RULE)
    fleet_svc._record_rule_would_have(
        db, _item(db), rule_id=T.UNDECLARED_FILES_RULE, outcome="would_hold", commit=None,
        branch="gb/x", reason="would have held: z.py")
    db.commit()
    assert [r.outcome for r in RO.history(db, db.pid, T.UNDECLARED_FILES_RULE)] == ["would_hold"]


def test_not_observing_records_nothing(db):
    out = fleet_svc._record_rule_would_have(
        db, _item(db), rule_id=T.UNDECLARED_FILES_RULE, outcome="would_hold", commit=None,
        branch="gb/x", reason="r")
    assert out["recorded"] is False
    assert RO.history(db, db.pid, T.UNDECLARED_FILES_RULE) == []


def test_unknown_rule_or_outcome_is_refused(db):
    for rule, outcome in (("build.test_scope", "would_hold"),
                          (T.GENERATED_FILES_RULE, "would_hold")):
        with pytest.raises(fleet_svc.ObservationError):
            fleet_svc._record_rule_would_have(db, _item(db), rule_id=rule, outcome=outcome,
                                              commit=None, branch="b", reason="r")


def test_brief_read_primitive_is_unmeasured_not_zero(db):
    for rid in (T.TEST_SCOPE_RULE, T.PATH_CLASSES_RULE):
        _observe(db, rid)
        seen = T.resolve(db, rid, db.pid)["observation"]
        assert seen["unmeasured"] is True
        assert seen["would_have"] is None and seen["would_hold"] is None


def test_observing_test_scope_brief_says_not_applied(db):
    item = _item(db)
    assert "Test scope: agent_full" in delegation.brief(db, item)["text"]
    _observe(db, T.TEST_SCOPE_RULE)
    out = delegation.brief(db, item)
    assert "Test scope: observing, not applied" in out["text"]
    assert "agent_full" not in out["text"]
    assert out["test_scope"] == T.TEST_SCOPE_DEFAULT
