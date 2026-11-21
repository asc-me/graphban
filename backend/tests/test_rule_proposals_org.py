"""GRPH-1808: org proposals REST — list across projects, batch adopt, org-scope dismiss."""
import pytest

from tests.test_custom_rules_org import _db, _events, _user, org  # noqa: F401
from tests.test_rule_proposals import _call, _login, _new_rule, env  # noqa: F401

URL = {"kind": "url", "ref": "https://example.com/why"}


def _propose(client, org, pid, slug):
    from app.services import rule_proposals as RP
    db = _db()
    try:
        p, _ = RP.propose(
            db, project_id=pid, proposer_kind="person", proposer="alex", kind="new_rule",
            slug=slug, primitive="exclusive_areas", title=slug.title(), why="clash",
            params={"paths": [f"db/{slug}"]}, citations=[URL])
        return p.id
    finally:
        db.close()


def _base(org):
    return f"/api/orgs/{org['org']}/rules/proposals"


def test_org_admin_lists_proposals_across_projects(client, org):
    a = _propose(client, org, org["pid"], "one")
    b = _propose(client, org, org["b"], "two")
    r = client.get(_base(org), headers=org["h"])
    assert r.status_code == 200, r.text
    got = {x["id"]: x["project_id"] for x in r.json()}
    assert got == {a: org["pid"], b: org["b"]}


def test_non_premium_org_is_refused(client, org):
    from app.models import Organization
    db = _db()
    try:
        db.get(Organization, org["org"]).plan = "free"
        db.commit()
    finally:
        db.close()
    r = client.get(_base(org), headers=org["h"])
    assert r.status_code == 402, r.text


def test_agent_key_refused_on_every_route(client, org):
    k = {"X-API-Key": org["key"]}
    pid = _propose(client, org, org["pid"], "one")
    calls = [client.get(_base(org), headers=k),
             client.post(_base(org) + "/adopt", json={"ids": [pid], "reason": "r"}, headers=k),
             client.post(f"{_base(org)}/{pid}/dismiss", json={"reason": "r"}, headers=k),
             client.get(f"/api/orgs/{org['org']}/rules/custom", headers=k)]
    for r in calls:
        assert r.status_code == 403 and "rules.person_adopts" in r.text, r.text


def test_batch_adopt_two_projects_one_event(client, org):
    a = _propose(client, org, org["pid"], "one")
    b = _propose(client, org, org["b"], "two")
    r = client.post(_base(org) + "/adopt", json={"ids": [a, b], "reason": "policy"},
                    headers=org["h"])
    assert r.status_code == 200, r.text
    from app.models import CustomRule
    db = _db()
    try:
        rows = db.query(CustomRule).filter(CustomRule.org_id == org["org"]).all()
        assert sorted((x.rule_id, x.state) for x in rows) == [
            ("custom.one", "observing"), ("custom.two", "observing")]
    finally:
        db.close()
    ev = _events("adopt_rule_proposals")
    assert len(ev) == 1 and sorted(ev[0].meta["projects"]) == sorted([org["pid"], org["b"]])
    assert sorted(ev[0].meta["projects_changed"]) == sorted([org["pid"], org["b"]])


def test_batch_with_a_loosening_adopts_nothing(client, org):
    from app.models import RuleProposal
    a = _propose(client, org, org["pid"], "one")
    b = _propose(client, org, org["b"], "two")
    db = _db()
    try:
        db.get(RuleProposal, b).loosens = True
        db.commit()
    finally:
        db.close()
    r = client.post(_base(org) + "/adopt", json={"ids": [a, b], "reason": "policy"},
                    headers=org["h"])
    assert r.status_code == 422 and str(b) in r.text
    from app.models import CustomRule
    db = _db()
    try:
        assert db.query(CustomRule).filter(CustomRule.org_id == org["org"]).count() == 0
        assert db.get(RuleProposal, a).status == "open"
    finally:
        db.close()


def test_adopt_lists_projects_that_override_the_rule(client, org):
    r = client.post(f"/api/projects/{org['pid']}/rules/custom", headers=org["h"], json={
        "slug": "one", "primitive": "exclusive_areas", "title": "One", "why": "w",
        "params": {"paths": ["mine"]}, "reason": "mine"})
    assert r.status_code == 201, r.text
    pid = _propose(client, org, org["b"], "one")
    dry = client.post(_base(org) + "/adopt", headers=org["h"],
                      json={"ids": [pid], "reason": "r", "dry_run": True})
    assert dry.status_code == 200, dry.text
    ov = dry.json()[0]["overrides"]
    assert [o["project_id"] for o in ov] == [org["pid"]] and ov[0]["params"] == {"paths": ["mine"]}
    from app.models import RuleProposal
    db = _db()
    try:
        assert db.get(RuleProposal, pid).status == "open"  # a dry read adopts nothing
    finally:
        db.close()
    real = client.post(_base(org) + "/adopt", headers=org["h"], json={"ids": [pid], "reason": "r"})
    assert real.status_code == 200 and [o["project_id"] for o in real.json()[0]["overrides"]] == [
        org["pid"]]


def test_org_dismiss_leaves_the_proposal_open_in_its_project(client, org):
    pid = _propose(client, org, org["pid"], "one")
    r = client.post(f"{_base(org)}/{pid}/dismiss", json={"reason": "not for us"},
                    headers=org["h"])
    assert r.status_code == 200, r.text
    assert pid not in [x["id"] for x in client.get(_base(org), headers=org["h"]).json()]
    dis = client.get(_base(org) + "?status=dismissed", headers=org["h"]).json()
    assert [x["id"] for x in dis] == [pid] and dis[0]["org_dismissed_reason"] == "not for us"
    mine = client.get(f"/api/projects/{org['pid']}/rules/proposals", headers=org["h"]).json()
    p = next(x for x in mine if x["id"] == pid)
    assert p["status"] == "open" and p["org_dismissed_by"] and p["org_dismissed_reason"] == "not for us"
    adopt = client.post(f"/api/projects/{org['pid']}/rules/proposals/adopt", headers=org["h"],
                        json={"proposal_ids": [pid], "reason": "we want it"})
    assert adopt.status_code == 200, adopt.text


def test_project_admin_who_is_not_org_admin_is_403(client, org):
    from app.models import Membership, OrgMembership, User
    db = _db()
    try:
        u = db.query(User).filter(User.email != "alex@ascme-labs.com").first()
        if u is None:
            u = User(email="p@x.io", name="P", handle="pp", password_hash="x")
            db.add(u)
            db.flush()
        db.add(OrgMembership(org_id=org["org"], user_id=u.id, role="member", handle="pp"))
        db.add(Membership(user_id=u.id, project_id=org["pid"], role="admin", access="write"))
        db.commit()
        from fastapi import HTTPException
        from app.services import rule_proposals as RP
        with pytest.raises(HTTPException) as e:
            RP.list_for_org(db, u, org["org"])
        assert e.value.status_code == 403
    finally:
        db.close()


def test_locking_an_observing_org_rule_is_refused_until_promoted(client, org):
    from app.errors import Validation
    from app.services import custom_rules as C, rule_toggles as T
    pid = _propose(client, org, org["pid"], "one")
    client.post(_base(org) + "/adopt", json={"ids": [pid], "reason": "r"}, headers=org["h"])
    db = _db()
    try:
        u = _user(db, org["uid"])
        with pytest.raises(Validation) as e:
            T.set_org_toggle(db, org["org"], "custom.one", {"locked": True}, actor=u, reason="l")
        assert "observing" in str(e.value)
        C.promote_org(db, u, org["org"], "custom.one", "ok")
        out = T.set_org_toggle(db, org["org"], "custom.one", {"locked": True}, actor=u, reason="l")
        assert out["locked"] is True
    finally:
        db.close()


def test_project_list_carries_who_decided(client, org):
    """The Rules page names the adopter and shows who dismissed, from the list route."""
    pid = _propose(client, org, org["pid"], "one")
    client.post(f"/api/projects/{org['pid']}/rules/proposals/{pid}/dismiss", headers=org["h"],
                json={"reason": "later"})
    got = client.get(f"/api/projects/{org['pid']}/rules/proposals?status=dismissed",
                     headers=org["h"]).json()[0]
    assert got["decided_by"] and got["decided_reason"] == "later" and got["decided_at"]
