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


def test_drift_clean_then_weakening_when_a_locked_pin_is_turned_off(client, org):
    _apply(client, org)
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "clean" and d["drift"] == []
    # The lock stays on, so only the on -> off check can report this.
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": False, "reason": "loosen"}, headers=org["owner"])
    assert _toggles(client, {"owner": org["owner"]}, org["ps"][0])[RULE]["locked"] is True
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "drifted" and len(d["drift"]) == 3
    assert {(x["rule_id"], x["kind"]) for x in d["drift"]} == {(RULE, "weakening")}
    assert not any("unlocked" in x["detail"] for x in d["drift"])


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


def test_org_recommendation_drops_an_adopted_rule_without_a_refresh(client, org):
    _observe(org["ps"][0])
    _observe(org["ps"][1])
    assert [s["rule_id"] for s in client.get(
        _url(org, "recommended"), headers=org["owner"]).json()["suggestions"]] == [RULE]
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": True, "reason": "adopt"}, headers=org["owner"])
    assert client.get(_url(org, "recommended"), headers=org["owner"]).json()["suggestions"] == []
    _apply(client, org)  # adopting through a profile is read live too
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": False, "locked": False, "reason": "drop"}, headers=org["owner"])
    assert [s["rule_id"] for s in client.get(
        _url(org, "recommended"), headers=org["owner"]).json()["suggestions"]] == [RULE]


def test_drift_reports_a_removed_lock_as_weakening(client, org):
    _apply(client, org)
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"locked": False, "reason": "let projects choose"}, headers=org["owner"])
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "drifted" and len(d["drift"]) == 3
    assert {(x["rule_id"], x["kind"]) for x in d["drift"]} == {(RULE, "weakening")}
    assert all("unlocked" in x["detail"] for x in d["drift"])


def test_drift_an_off_pin_turned_on_is_changed_not_weakening(client, org, monkeypatch):
    prof = P.CURRENT.profiles["standard"]
    monkeypatch.setitem(prof, "rules", {**prof["rules"], OTHER: {"state": "off"}})
    _apply(client, org)
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{OTHER}",
               json={"on": True, "reason": "tighten"}, headers=org["owner"])
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert {(x["rule_id"], x["kind"]) for x in d["drift"]} == {(OTHER, "changed")}


def _bad_last_pin(monkeypatch, pin):
    # OTHER sorts before RULE, so a write-as-you-go apply would land OTHER before RULE fails.
    assert OTHER < RULE
    monkeypatch.setitem(P.CURRENT.profiles["standard"], "rules",
                        {OTHER: {"state": "on"}, RULE: pin})


def test_apply_is_all_or_nothing_on_a_bad_param(client, org, monkeypatch):
    _bad_last_pin(monkeypatch, {"state": "on", "params": {"zzz": 1}})
    r = _apply(client, org)
    assert r.status_code == 422 and "zzz" in r.text
    for pid in org["ps"]:
        assert _toggles(client, {"owner": org["owner"]}, pid)[OTHER]["on"]["source"] \
            == "platform default"


def test_apply_is_all_or_nothing_on_a_bad_state(client, org, monkeypatch):
    _bad_last_pin(monkeypatch, {"state": "maybe"})
    r = _apply(client, org)
    assert r.status_code == 422 and "maybe" in r.text
    for pid in org["ps"]:
        assert _toggles(client, {"owner": org["owner"]}, pid)[OTHER]["on"]["source"] \
            == "platform default"


def test_profile_naming_an_unmeasured_rule_applies_nothing_and_drifts_unmeasured(client, org):
    T.register(T.LibraryRule(
        id="review.unmeasured", stage="review", title="u", summary="s", enforcement="enforced",
        enforcer=R.Enforcer("doc", "x"), default_on=False, default_params={},
        unmeasured_reason="no primitive"))
    P.CURRENT.profiles["um"] = {"id": "um", "rules": {OTHER: {"state": "on"},
                                                      "review.unmeasured": {"state": "on"}}}
    try:
        r = _apply(client, org, "um")
        assert r.status_code == 422 and "no primitive" in r.text
        assert _toggles(client, {"owner": org["owner"]}, org["ps"][0])[OTHER]["on"]["source"] \
            == "platform default"
        d = client.get(_url(org, "profiles/um/drift"), headers=org["owner"]).json()
        assert {"rule_id": "review.unmeasured", "project_id": None, "kind": "unmeasured",
                "detail": "the loaded library cannot enforce this rule"} in d["drift"]
    finally:
        P.CURRENT.profiles.pop("um")


def test_drift_unmeasured_for_a_rule_not_in_the_library(client, org):
    d = client.get(_url(org, "profiles/bad/drift"), headers=org["owner"]).json()
    assert d["state"] == "drifted"
    assert [x for x in d["drift"] if x["kind"] == "unmeasured"] == [
        {"rule_id": "nope.rule", "project_id": None, "kind": "unmeasured",
         "detail": "the loaded library cannot enforce this rule"}]


def test_drift_a_params_change_is_changed_with_a_params_detail(client, org):
    _apply(client, org)
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": True, "locked": True, "params": {"n": 7}, "reason": "retune"},
               headers=org["owner"])
    d = client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).json()
    assert d["state"] == "drifted" and len(d["drift"]) == 3
    assert {(x["rule_id"], x["kind"]) for x in d["drift"]} == {(RULE, "changed")}
    assert all("pinned params" in x["detail"] for x in d["drift"])


def test_org_recommendation_floor_rises_to_a_fifth_of_active_projects(client, org, monkeypatch):
    for pid in org["ps"][:2]:
        _observe(pid)
    monkeypatch.setattr(RO, "active_projects", lambda db, oid: [f"x{i}" for i in range(11)])
    r = client.get(_url(org, "recommended"), headers=org["owner"]).json()
    assert r["org_min_projects"] == 3 and r["active_projects"] == 11
    assert r["suggestions"] == []  # 2 projects carry it, below a floor of 3


def test_active_projects_are_those_with_an_item_moved_in_30_days(client, org):
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    db = _db()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=31)
        new = datetime.now(timezone.utc) - timedelta(days=1)
        for i, (pid, at) in enumerate([(org["ps"][0], new), (org["ps"][1], old)]):
            db.add(Item(id=f"ACT-{i}", number=i + 1, project_id=pid, title="t", updated_at=at))
        db.commit()
        assert RO.active_projects(db, org["id"]) == [org["ps"][0]]
    finally:
        db.close()


def test_org_recommendation_recomputes_after_a_week_and_reports_its_age(client, org, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(RO.time, "monotonic", lambda: now[0])
    assert client.get(_url(org, "recommended"), headers=org["owner"]).json()["suggestions"] == []
    _observe(org["ps"][0])
    _observe(org["ps"][1])
    rr.clear_cache()
    now[0] += 3600
    r = client.get(_url(org, "recommended"), headers=org["owner"]).json()
    assert r["suggestions"] == [] and r["age_seconds"] == 3600
    now[0] += RO.CACHE_TTL_SECONDS
    r = client.get(_url(org, "recommended"), headers=org["owner"]).json()
    assert [s["rule_id"] for s in r["suggestions"]] == [RULE] and r["age_seconds"] == 0


@pytest.mark.parametrize("plan,code", [("enterprise", 200), ("free", 402)])
def test_profiles_and_drift_are_plan_gated(client, org, plan, code):
    _set_plan(org["id"], plan)
    assert client.get(_url(org, "profiles"), headers=org["owner"]).status_code == code
    assert client.get(_url(org, "profiles/standard/drift"), headers=org["owner"]).status_code == code


# ---- apply to one project (GRPH-1291, D11, D13) -------------------------------------------

def _apply_project(client, org, pid, profile="standard", reason="adopt", headers=None):
    return client.post(f"/api/rules/profiles/{profile}/apply", params={"project_id": pid},
                       json={"reason": reason}, headers=headers or org["owner"])


def test_apply_to_one_project_sets_toggles_and_audits_once(client, org):
    p, other = org["ps"][0], org["ps"][1]
    r = _apply_project(client, org, p)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == sorted([RULE, OTHER])
    t = _toggles(client, {"owner": org["owner"]}, p)[RULE]
    assert t["on"] == {"value": True, "source": "project"}
    assert t["params"]["value"] == {"n": 3} and t["locked"] is False
    assert _toggles(client, {"owner": org["owner"]}, other)[RULE]["on"]["value"] is False
    from app.models import Event

    db = _db()
    evs = db.query(Event).filter(Event.action == "apply_rule_profile").all()
    assert len(evs) == 1 and evs[0].project_id == p
    assert evs[0].meta["profile"] == "standard" and evs[0].meta["profile_version"] == "1"


def test_apply_to_a_project_is_refused_whole_when_the_org_locks_a_rule(client, org):
    # Lock the rule that sorts LAST, so a write loop that refuses per rule would already have
    # written OTHER: only the up-front lock check keeps the apply from being half done.
    assert OTHER < RULE
    client.put(f"/api/orgs/{org['id']}/rule-toggles/{RULE}",
               json={"on": False, "locked": True, "reason": "freeze"}, headers=org["owner"])
    p = org["ps"][0]
    r = _apply_project(client, org, p)
    assert r.status_code == 409, r.text
    assert RULE in r.text and OTHER not in r.json()['detail'].split('locked')[1]
    toggles = _toggles(client, {"owner": org["owner"]}, p)
    assert toggles[OTHER]["on"]["source"] != "project"
    assert toggles[RULE]["on"]["value"] is False


def test_apply_to_a_project_is_refused_for_an_api_key_or_the_engine(client, org):
    from fastapi import HTTPException

    from app.models import ApiKey

    db = _db()
    for actor in (ApiKey(id="k1", project_id=org["ps"][0]), ("engine", "recommend")):
        with pytest.raises(HTTPException) as e:
            RO.apply_project_profile(db, actor, org["ps"][0], "standard", reason="x")
        assert e.value.status_code == 403 and e.value.rule_id == T.PERSON_RULE


def test_project_drift_counts_a_changed_rule_once_and_flags_weakening(client, org):
    p = org["ps"][0]
    _apply_project(client, org, p)
    d = client.get("/api/rules/profiles/standard/drift", params={"project_id": p},
                   headers=org["owner"]).json()
    assert d["state"] == "clean" and d["differs_in"] == 0
    client.put(f"/api/rules/toggles/{RULE}", params={"project_id": p},
               json={"on": False, "params": {"n": 9}, "reason": "loosen"}, headers=org["owner"])
    d = client.get("/api/rules/profiles/standard/drift", params={"project_id": p},
                   headers=org["owner"]).json()
    assert d["differs_in"] == 1 and d["weakening"] == 1
    assert d["summary"] == "differs from Standard in 1 rule"
    assert d["drift"][0]["kind"] == "weakening"
