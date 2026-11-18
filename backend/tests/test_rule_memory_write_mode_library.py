"""GRPH-1603: `memory.write_mode` as a library rule, backed by `Project.memory_write_mode`.

The column is the one value: the rules view and the project endpoint read and write it, a
profile applies it like any other rule, and an org lock refuses a project change on either path.
"""
import pytest

from app.models import Project, RuleToggle
from app.services import memory
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_rule_toggles import _db, _login, _owner, _put_org, _set_plan, _toggles

RULE = T.MEMORY_WRITE_MODE_RULE


@pytest.fixture()
def org(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(T, "LIBRARY", {})
    prim = P.PRIMITIVES["memory_write_mode"]
    T.register(T.LibraryRule(
        id=RULE, stage="learn", title="Memory write mode", summary="s",
        enforcement=prim.level, enforcer=prim.enforcer, default_on=True,
        default_params={"mode": "review"}))
    monkeypatch.setattr(P, "CURRENT", P.Pack(
        "t", "1.0.0", P.PRIMITIVE_API, "0.0.0", {}, {},
        {"exploratory": {"id": "exploratory", "title": "Exploratory", "version": "1",
                         "rules": {RULE: {"state": "on", "params": {"mode": "auto"}}}},
         "regulated": {"id": "regulated", "title": "Regulated", "version": "1",
                       "rules": {RULE: {"state": "on", "params": {"mode": "review"}}}}},
        []))
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    a = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()["id"]
    _set_plan(o["id"], "team")
    return {"owner": owner, "id": o["id"], "a": a}


def _project_mode(client, org, pid):
    r = client.get("/api/projects", headers=org["owner"])
    assert r.status_code == 200, r.text
    return next(p for p in r.json() if p["id"] == pid)["memory_write_mode"]


def _rules_mode(client, org, pid):
    t = _toggles(client, org, pid)[RULE]
    return t["params"]["value"]["mode"], t["params"]["source"]


def _put_rule(client, org, pid, body):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": pid}, json=body,
                      headers=org["owner"])


def _toggle_rows(pid):
    db = _db()
    try:
        return db.query(RuleToggle).filter(RuleToggle.rule_id == RULE,
                                           RuleToggle.project_id == pid).count()
    finally:
        db.close()


def _rule_history(pid):
    from app.models import Event
    db = _db()
    try:
        return [e.meta for e in db.query(Event).filter(
            Event.action == "set_rule_toggle", Event.target_id == RULE,
            Event.project_id == pid).order_by(Event.id)]
    finally:
        db.close()


def test_the_primitive_is_published_with_a_mode_param_and_the_api_is_bumped():
    c = P.contract()
    prim = next(p for p in c["primitives"] if p["id"] == "memory_write_mode")
    assert prim["params"] == [{"name": "mode", "type": "str", "required": False}]
    assert P._ver(c["api_version"]) >= (1, 10, 0)
    t = {"id": RULE, "stage": "Learn", "title": "t", "summary": "s",
         "primitive": "memory_write_mode", "claims": "enforced", "params": {"mode": "review"}}
    assert P._check_template(t, P.PRIMITIVE_API) == ""


def test_both_directions_hold_one_value(client, org):
    pid = org["a"]
    assert _project_mode(client, org, pid) == "review"
    assert _rules_mode(client, org, pid) == ("review", "project")

    # Project endpoint -> rules view.
    r = client.put(f"/api/projects/{pid}/memory-write-mode",
                   json={"mode": "trusted", "reason": "solo"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert _rules_mode(client, org, pid) == ("trusted", "project")

    # Rules view -> project endpoint.
    r = _put_rule(client, org, pid, {"params": {"mode": "auto"}, "reason": "judge it"})
    assert r.status_code == 200, r.text
    assert _project_mode(client, org, pid) == "auto"
    assert _rules_mode(client, org, pid) == ("auto", "project")
    # The column is the only copy: no toggle row was written for the project.
    assert _toggle_rows(pid) == 0
    hist = _rule_history(pid)
    assert hist[-1]["reason"] == "judge it" and hist[-1]["new"]["params"] == {"mode": "auto"}

    # The memory gate reads the same value.
    db = _db()
    try:
        assert memory._triage_prefs(db, pid)[0] == "auto"
    finally:
        db.close()


def test_an_unknown_mode_or_off_is_refused_and_not_stored(client, org):
    pid = org["a"]
    r = _put_rule(client, org, pid, {"params": {"mode": "bogus"}, "reason": "x"})
    assert r.status_code == 422, r.text
    r = _put_rule(client, org, pid, {"on": False, "reason": "x"})
    assert r.status_code == 422, r.text
    assert _project_mode(client, org, pid) == "review" and _toggle_rows(pid) == 0


def test_a_profile_sets_the_mode_through_the_profile_apply(client, org):
    pid = org["a"]
    r = client.post("/api/rules/profiles/exploratory/apply", params={"project_id": pid},
                    json={"reason": "spike"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == [RULE]
    assert _project_mode(client, org, pid) == "auto"
    assert _rules_mode(client, org, pid) == ("auto", "project")
    assert _toggle_rows(pid) == 0
    ev = _rule_history(pid)[-1]
    assert ev["reason"] == "spike"
    assert ev["old"]["params"] == {"mode": "review"} and ev["new"]["params"] == {"mode": "auto"}

    r = client.post("/api/rules/profiles/regulated/apply", params={"project_id": pid},
                    json={"reason": "audit"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert _project_mode(client, org, pid) == "review"
    d = client.get("/api/rules/profiles/regulated/drift", params={"project_id": pid},
                   headers=org["owner"]).json()
    assert d["state"] == "clean", d


def test_an_org_lock_refuses_a_project_change_on_both_paths(client, org):
    pid = org["a"]
    assert _put_org(client, org, {"params": {"mode": "review"}, "locked": True,
                                  "reason": "regulated"}, rule_id=RULE).status_code == 200
    t = _toggles(client, org, pid)[RULE]
    assert t["locked"] is True and t["params"] == {"value": {"mode": "review"}, "source": "org"}

    r = _put_rule(client, org, pid, {"params": {"mode": "trusted"}, "reason": "x"})
    assert r.status_code == 409 and T.LOCK_RULE in r.text, r.text
    r = client.put(f"/api/projects/{pid}/memory-write-mode",
                   json={"mode": "trusted", "reason": "x"}, headers=org["owner"])
    assert r.status_code == 409 and T.LOCK_RULE in r.text, r.text
    r = client.patch(f"/api/projects/{pid}", json={"memory_write_mode": "trusted"},
                     headers=org["owner"])
    assert r.status_code == 409, r.text
    assert _project_mode(client, org, pid) == "review"


def test_a_lock_governs_the_gate_and_unlocking_gives_the_project_its_mode_back(client, org):
    pid = org["a"]
    client.put(f"/api/projects/{pid}/memory-write-mode",
               json={"mode": "trusted", "reason": "solo"}, headers=org["owner"])
    _put_org(client, org, {"params": {"mode": "review"}, "locked": True, "reason": "lock"},
             rule_id=RULE)
    db = _db()
    try:
        assert T.memory_write_mode(db, pid) == "review"
    finally:
        db.close()
    _put_org(client, org, {"locked": False, "reason": "unlock"}, rule_id=RULE)
    assert _rules_mode(client, org, pid) == ("trusted", "project")


def test_the_registry_draws_the_rule_once_from_the_library(client, org):
    db = _db()
    try:
        rows = [r for r in R.registry(db, org["a"]) if r["id"] == RULE]
    finally:
        db.close()
    assert len(rows) == 1 and rows[0]["control"] == "toggle"
    assert rows[0]["params"]["value"] == {"mode": "review"}


def test_a_stray_toggle_row_is_never_read(client, org):
    """Sabotage guard: a project toggle row on this rule is not the value."""
    pid = org["a"]
    db = _db()
    try:
        db.add(RuleToggle(rule_id=RULE, project_id=pid, params={"mode": "trusted"},
                          updated_by=_owner(db).id))
        db.commit()
        assert db.get(Project, pid).memory_write_mode == "review"
    finally:
        db.close()
    assert _rules_mode(client, org, pid) == ("review", "project")
