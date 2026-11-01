"""PRD-P63 S3 (GRPH-1263): project recommendations from observations (D5, D7)."""
import pytest

from app.db import SessionLocal
from app.models import Observation
from app.services import observations as obs
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as rules_svc
from app.services import rules_recommend as rr
from tests.test_observations import _item, _signoff

PID = "core"
RULE = "review.sabotage_required"


@pytest.fixture()
def db(client):
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _pack(client):
    saved = (dict(T.LIBRARY), P.CURRENT)
    T.LIBRARY.clear()
    T.register(T.LibraryRule(id=RULE, stage="review", title="Sabotage required",
                             summary="Sign-off needs a reviewer sabotage.",
                             enforcement="enforced", enforcer=rules_svc.Enforcer("doc", "x")))
    P.CURRENT = P.Pack("t", "1.0.0", "1.0.0", "0.0.0", {}, {}, {},
                       [{"observation": "predicate_exempted", "rule": RULE, "weight": 2,
                         "replay": True}])
    rr.clear_cache()
    yield
    T.LIBRARY.clear()
    T.LIBRARY.update(saved[0])
    P.CURRENT = saved[1]
    rr.clear_cache()


def _exempt(db, n):
    for _ in range(n):
        obs.on_sign_off(db, _item(db, effort=0, evidence=[_signoff()]), [])
    rr.clear_cache()


def _rec(client, auth):
    return client.get(f"/api/rules/recommended?project_id={PID}", headers=auth).json()


def _rule_sug(body):
    return [s for s in body["suggestions"] if s["kind"] == "rule"]


def test_mapped_observations_suggest_the_rule_with_links_score_and_replay(client, auth, db):
    _exempt(db, 3)
    (s,) = _rule_sug(_rec(client, auth))
    assert s["rule_id"] == RULE and s["id"] == f"suggest.rule.{RULE}"
    assert s["score"] > 0 and s["effect"]
    assert s["replay"]["would_differ"] == 3 and s["replay"]["signed_off"] >= 3
    assert len(s["evidence"]) == 4 and all(e["link"] for e in s["evidence"][1:])


def test_one_observation_is_not_a_pattern(client, auth, db):
    _exempt(db, 1)
    assert _rule_sug(_rec(client, auth)) == []


def test_rule_already_on_is_not_suggested(client, auth, db):
    _exempt(db, 3)
    T.register(T.LibraryRule(id=RULE, stage="review", title="t", summary="s",
                             enforcement="enforced", enforcer=rules_svc.Enforcer("doc", "x"),
                             default_on=True))
    assert _rule_sug(_rec(client, auth)) == []


def test_dismissal_holds_until_new_evidence(client, auth, db):
    _exempt(db, 2)
    sid = f"suggest.rule.{RULE}"
    assert client.post(f"/api/rules/recommended/{sid}/dismiss?project_id={PID}",
                       headers=auth).status_code in (200, 204)
    assert _rule_sug(_rec(client, auth)) == []
    _exempt(db, 1)
    assert len(_rule_sug(_rec(client, auth))) == 1


def test_unmatched_kind_becomes_a_redacted_proposal(client, auth, db):
    for i in range(3):
        db.add(Observation(project_id=PID, kind="reopened_after_done", stage="merge",
                           dedupe_key=f"k{i}", ref={"x": 1}))
    db.commit()
    rr.clear_cache()
    (p,) = [s for s in _rec(client, auth)["suggestions"] if s["kind"] == "proposal"]
    assert p["report"] == {"kind": "reopened_after_done", "stage": "merge", "count": 3,
                           "items": 0, "tag": "rule-library"}


def test_unloadable_rule_is_a_proposal_not_a_suggestion(client, auth, db):
    T.register(T.LibraryRule(id=RULE, stage="review", title="t", summary="s",
                             enforcement="unmeasured", enforcer=rules_svc.Enforcer("doc", "x"),
                             unmeasured_reason="refused"))
    _exempt(db, 3)
    body = _rec(client, auth)
    assert _rule_sug(body) == []
    assert [s["id"] for s in body["suggestions"]] == ["propose.predicate_exempted"]


def test_engine_never_changes_a_toggle(client, auth, db):
    _exempt(db, 3)
    before = T.resolve(db, RULE, PID)
    _rec(client, auth)
    assert T.resolve(db, RULE, PID) == before
