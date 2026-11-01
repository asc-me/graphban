"""PRD-P63 S4 (GRPH-1264): oversight profiles and org recommendations (D6, D11, D13)."""
import pytest

from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from app.services import rules_org as RO
from app.services import rules_recommend as rr
from tests.test_rule_toggles import RULE, _db, _login, _set_plan, _toggles

OTHER = "review.evidence_attached"


@pytest.fixture()
def org(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(T, "LIBRARY", {})
    for rid in (RULE, OTHER):
        T.register(T.LibraryRule(
            id=rid, stage="review", title=rid, summary="s", enforcement="enforced",
            enforcer=R.Enforcer("doc", "x"), default_on=False, default_params={"n": 1}))
    monkeypatch.setattr(P, "CURRENT", P.Pack(
        "t", "1.0.0", "1.0.0", "0.0.0", {}, {},
        {"standard": {"id": "standard", "title": "Standard", "version": "1",
                      "rules": {RULE: {"state": "on", "params": {"n": 3}},
                                OTHER: {"state": "on"}}},
         "bad": {"id": "bad", "rules": {RULE: {"state": "on"}, "nope.rule": {"state": "on"}}}},
        [{"observation": "predicate_exempted", "rule": RULE, "weight": 2}]))
    rr.clear_cache()
    RO.clear_cache()
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    ps = [client.post("/api/projects", json={"name": f"P{i}"}, headers=owner).json()["id"]
          for i in range(3)]
    _set_plan(o["id"], "team")
    return {"owner": owner, "id": o["id"], "ps": ps}


def _url(org, tail):
    return f"/api/orgs/{org['id']}/rules/{tail}"


def _apply(client, org, pid="standard", reason="adopt"):
    return client.post(_url(org, f"profiles/{pid}/apply"), json={"reason": reason},
                       headers=org["owner"])


def test_apply_pins_locked_values_in_every_project_with_audit(client, org):
    r = _apply(client, org)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == sorted([RULE, OTHER])
    for pid in org["ps"]:
        t = _toggles(client, {"owner": org["owner"]}, pid)[RULE]
        assert t["on"] == {"value": True, "source": "org"}
        assert t["params"]["value"] == {"n": 3} and t["locked"] is True
    from app.models import Event

    db = _db()
    try:
        acts = [e.action for e in db.query(Event).order_by(Event.id)
                if e.action in ("set_rule_toggle", "apply_rule_profile")]
    finally:
        db.close()
    assert acts.count("set_rule_toggle") == 2 and acts.count("apply_rule_profile") == 1


def test_profile_naming_an_unenforceable_rule_applies_nothing(client, org):
    r = _apply(client, org, "bad")
    assert r.status_code == 422 and "nope.rule" in r.text
    assert _toggles(client, {"owner": org["owner"]}, org["ps"][0])[RULE]["on"]["source"] \
        == "platform default"


def test_apply_needs_reason_unknown_profile_and_plan(client, org):
    assert _apply(client, org, reason="").status_code == 422
    assert _apply(client, org, "ghost").status_code == 404
    _set_plan(org["id"], "free")
    assert _apply(client, org).status_code == 402
    assert client.get(_url(org, "recommended"), headers=org["owner"]).status_code == 402
    assert client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).status_code == 402


def test_drift_clean_then_weakening_when_the_org_unpins(client, org):
    _apply(client, org)
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "clean" and d["drift"] == []
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": False, "locked": False, "reason": "loosen"}, headers=org["owner"])
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "drifted"
    assert {(x["rule_id"], x["kind"]) for x in d["drift"]} == {(RULE, "weakening")}
    assert len(d["drift"]) == 3


def test_list_profiles(client, org):
    r = client.get(_url(org, "profiles"), headers=org["owner"]).json()
    assert r["pack_loaded"] and {p["id"] for p in r["profiles"]} == {"standard", "bad"}


def test_org_min_projects_is_the_larger_of_two_and_a_fifth():
    assert [RO.org_min_projects(n) for n in (0, 1, 5, 10, 11, 30)] == [2, 2, 2, 2, 3, 6]


def _observe(pid, n=2):
    from app.models import Observation

    db = _db()
    try:
        for i in range(n):
            db.add(Observation(project_id=pid, kind="predicate_exempted", stage="review",
                               dedupe_key=f"k{i}", ref={}))
        db.commit()
    finally:
        db.close()


def test_org_recommendation_needs_the_floor_of_projects(client, org):
    _observe(org["ps"][0])
    r = client.get(_url(org, "recommended"), headers=org["owner"]).json()
    assert r["suggestions"] == [] and r["org_min_projects"] == 2
    assert len(r["projects_without_observations"]) == 2  # absence is reported, not hidden
    _observe(org["ps"][1])
    r = client.get(_url(org, "recommended"), params={"refresh": True}, headers=org["owner"]).json()
    s = r["suggestions"]
    assert [x["rule_id"] for x in s] == [RULE] and s[0]["projects"] == 2
    assert r["age_seconds"] >= 0 and r["computed_at"]


def test_org_recommendation_drops_a_rule_the_org_has_on(client, org):
    _observe(org["ps"][0])
    _observe(org["ps"][1])
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": True, "reason": "adopt"}, headers=org["owner"])
    r = client.get(_url(org, "recommended"), params={"refresh": True}, headers=org["owner"]).json()
    assert r["suggestions"] == []


def test_org_recommendation_is_cached_weekly(client, org):
    client.get(_url(org, "recommended"), headers=org["owner"])
    _observe(org["ps"][0])
    _observe(org["ps"][1])
    rr.clear_cache()
    assert client.get(_url(org, "recommended"), headers=org["owner"]).json()["suggestions"] == []
