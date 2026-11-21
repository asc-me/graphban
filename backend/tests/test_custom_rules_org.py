"""P91 S7 (GRPH-1779): org-scope custom rules — one org-owned row, resolved, never copied."""
import pytest

from app.errors import Refused
from app.services import custom_rules as C
from app.services import rule_proposals as RP
from app.services import rule_toggles as T
from tests.test_rule_proposals import _call, _login, _new_rule, env  # noqa: F401

BODY = {"slug": "areas", "primitive": "exclusive_areas", "title": "Areas",
        "why": "migrations clash", "params": {"paths": ["db/migrations"]}, "reason": "clashes"}


def _db():
    from app.db import SessionLocal
    return SessionLocal()


@pytest.fixture()
def org(client, env, monkeypatch):  # noqa: F811
    """An org on the `team` plan holding the proposal env's project and a second one."""
    from app.config import settings
    from app.models import Organization, Project, User
    monkeypatch.setattr(settings, "hosted_mode", True)
    h = env["h"]
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=h).json()
    b = client.post("/api/projects", json={"name": "Probe"}, headers=h).json()["id"]
    db = _db()
    try:
        db.get(Organization, o["id"]).plan = "team"
        for pid in (env["pid"], b):
            db.get(Project, pid).org_id = o["id"]
        db.commit()
        uid = db.query(User).filter(User.email == "alex@ascme-labs.com").one().id
    finally:
        db.close()
    return {**env, "org": o["id"], "b": b, "uid": uid}


def _user(db, uid):
    from app.models import User
    return db.get(User, uid)


def _events(action, **filters):
    from app.models import Event
    db = _db()
    try:
        return [e for e in db.query(Event).filter(Event.action == action) if all(
            (e.meta or {}).get(k) == v for k, v in filters.items())]
    finally:
        db.close()


def test_org_adopt_needs_the_org_rules_entitlement(client, org):
    from app.errors import QuotaExceeded
    from app.models import Organization
    db = _db()
    try:
        db.get(Organization, org["org"]).plan = "free"
        db.commit()
        with pytest.raises(QuotaExceeded):
            C.create_org(db, _user(db, org["uid"]), org["org"], **BODY)
    finally:
        db.close()


def test_org_rule_resolves_in_two_projects_without_copies(client, org):
    db = _db()
    try:
        C.create_org(db, _user(db, org["uid"]), org["org"], **BODY)
        for pid in (org["pid"], org["b"]):
            s = T.resolve(db, "custom.areas", pid)
            assert s["on"] == {"value": True, "source": "org"} and s["override"] is False
        from app.models import CustomRule
        rows = db.query(CustomRule).filter(CustomRule.rule_id == "custom.areas").all()
        assert [(r.org_id, r.project_id) for r in rows] == [(org["org"], None)]
    finally:
        db.close()
    ev = _events("create_org_custom_rule")
    assert len(ev) == 1 and sorted(ev[0].meta["projects_changed"]) == sorted([org["pid"], org["b"]])


def test_project_first_then_org_keeps_override_and_lock_retires_it(client, org):
    r = client.post(f"/api/projects/{org['pid']}/rules/custom", json=BODY, headers=org["h"])
    assert r.status_code == 201, r.text
    db = _db()
    try:
        user = _user(db, org["uid"])
        C.create_org(db, user, org["org"], **{**BODY, "params": {"paths": ["org/path"]}})
        mine = T.resolve(db, "custom.areas", org["pid"])
        assert mine["override"] is True and mine["on"]["source"] == "custom"
        assert mine["params"]["value"] == {"paths": ["db/migrations"]}
        assert T.resolve(db, "custom.areas", org["b"])["params"]["value"] == {"paths": ["org/path"]}
        out = T.set_org_toggle(db, org["org"], "custom.areas", {"locked": True}, actor=user,
                               reason="policy")
        assert out["retired_overrides"][0]["project_id"] == org["pid"]
        locked = T.resolve(db, "custom.areas", org["pid"])
        assert locked["locked"] is True and locked["on"]["source"] == "org"
        assert locked["params"]["value"] == {"paths": ["org/path"]}
        with pytest.raises(Refused) as e:
            C.refuse_if_locked(db, "custom.areas", org["pid"])
        assert e.value.rule_id == T.LOCK_RULE
    finally:
        db.close()
    hist = [e for e in _events("retire_rule_override") if e.project_id == org["pid"]]
    assert len(hist) == 1 and T.LOCK_RULE in hist[0].meta["history"]
    # a project write against the locked rule is refused over REST, naming the rule
    r = client.post(f"/api/projects/{org['pid']}/rules/custom", json=BODY, headers=org["h"])
    assert r.status_code == 409 and T.LOCK_RULE in r.text


def test_org_admin_adopts_a_project_dismissed_proposal(client, org):
    pid = _call(client, org["key"], "propose_rule", _new_rule(org))["structuredContent"]["id"]
    d = client.post(f"/api/projects/{org['pid']}/rules/proposals/{pid}/dismiss", headers=org["h"],
                    json={"reason": "not now"})
    assert d.status_code == 200, d.text
    db = _db()
    try:
        p = RP.adopt_org(db, _user(db, org["uid"]), org["org"], pid, "org policy")
        assert p.status == "adopted"
        s = T.resolve(db, "custom.areas", org["b"])
        assert s["state"] == "observing" and s["on"]["source"] == "org"
        assert "observation" in s
    finally:
        db.close()
    dismissed = _events("dismiss_rule_proposal")
    adopted = _events("adopt_rule_proposal", scope="org")
    assert dismissed[0].meta["reason"] == "not now"
    assert adopted[0].meta["reason"] == "org policy" and "dismissed by" in adopted[0].meta["history"]


def test_one_audit_event_names_the_changed_projects(client, org):
    db = _db()
    try:
        user = _user(db, org["uid"])
        C.create_org(db, user, org["org"], **BODY)
        T.set_org_toggle(db, org["org"], "custom.areas", {"locked": True}, actor=user,
                         reason="lock")
    finally:
        db.close()
    ev = _events("set_rule_toggle", scope="org")
    assert len(ev) == 1 and ev[0].meta["new"] == {"locked": True}
    assert len(_events("create_org_custom_rule")) == 1


def test_agent_cannot_write_org_rule(client, org):
    db = _db()
    try:
        from app.models import ApiKey
        key = db.query(ApiKey).first()
        with pytest.raises(Refused) as e:
            C.create_org(db, key, org["org"], **BODY)
        assert e.value.rule_id == T.PERSON_RULE
    finally:
        db.close()


def _tighten_org_rule(db, org):
    """A project proposal to tighten the org-owned `custom.areas` — filed on a project only."""
    C.create_org(db, _user(db, org["uid"]), org["org"], **BODY)
    p, folded = RP.propose(db, project_id=org["pid"], proposer_kind="agent", proposer="GRPH-A1",
                           kind="tighten", rule_id="custom.areas", citations=org["cites"],
                           params={"paths": ["db/migrations", "db/seeds"]})
    assert folded is False and p.primitive == "exclusive_areas"
    return p


def test_project_adopts_a_tighten_of_an_org_rule_as_a_kept_override(client, org):
    from app.models import CustomRule
    db = _db()
    try:
        p = _tighten_org_rule(db, org)
        RP.adopt(db, _user(db, org["uid"]), org["pid"], [p.id], "stricter here")
        mine = T.resolve(db, "custom.areas", org["pid"])
        assert mine["override"] is True and mine["on"]["source"] == "custom"
        assert mine["params"]["value"] == {"paths": ["db/migrations", "db/seeds"]}
        other = T.resolve(db, "custom.areas", org["b"])
        assert other["on"]["source"] == "org" and other["params"]["value"] == BODY["params"]
        org_row = C.get_org(db, org["org"], "custom.areas")
        assert org_row.params == BODY["params"]
        own = db.query(CustomRule).filter(CustomRule.rule_id == "custom.areas",
                                          CustomRule.project_id == org["pid"]).one()
        assert own.org_id is None
    finally:
        db.close()


def test_org_adopts_a_project_tighten_of_its_rule(client, org):
    from app.models import CustomRule
    db = _db()
    try:
        p = _tighten_org_rule(db, org)
        RP.adopt_org(db, _user(db, org["uid"]), org["org"], p.id, "everywhere")
        for pid in (org["pid"], org["b"]):
            s = T.resolve(db, "custom.areas", pid)
            assert s["on"]["source"] == "org" and s["override"] is False
            assert s["params"]["value"] == {"paths": ["db/migrations", "db/seeds"]}
        rows = db.query(CustomRule).filter(CustomRule.rule_id == "custom.areas").all()
        assert [(r.org_id, r.project_id) for r in rows] == [(org["org"], None)]
    finally:
        db.close()
    ev = _events("adopt_rule_proposal", scope="org")
    assert len(ev) == 1 and ev[0].meta["kind"] == "tighten"
