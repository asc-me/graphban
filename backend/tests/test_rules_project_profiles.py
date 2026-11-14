"""GRPH-1609: project-scoped read of the pack's profiles and the applied level; accept keeps exceptions."""
import pytest

from app.services import rules_org as RO
from tests.test_rules_org import (  # noqa: F401  (fixture re-export)
    OTHER, RULE, _apply_project, _events, _newer_pack, org,
)
from tests.test_rule_toggles import _login


def _get(client, org, pid, headers=None):
    return client.get("/api/rules/profiles", params={"project_id": pid},
                      headers=headers or org["owner"])


def test_reads_profiles_and_null_applied_before_any_apply(client, org):
    r = _get(client, org, org["ps"][0])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["applied"] is None and j["order"] == ["standard", "bad"]
    std = next(p for p in j["profiles"] if p["id"] == "standard")
    assert {p["rule_id"]: p["enforceable"] for p in std["pins"]} == {RULE: True, OTHER: True}
    bad = next(p for p in j["profiles"] if p["id"] == "bad")
    assert {p["rule_id"]: p["enforceable"] for p in bad["pins"]}["nope.rule"] is False


def test_applied_names_profile_version_and_exception(client, org):
    p = org["ps"][0]
    r = client.post("/api/rules/profiles/standard/apply", params={"project_id": p},
                    json={"reason": "x", "except": [OTHER]}, headers=org["owner"])
    assert r.status_code == 200, r.text
    a = _get(client, org, p).json()["applied"]
    assert (a["profile"], a["version"], a["exceptions"]) == ("standard", "1", [OTHER])


def test_outsider_is_refused(client, org):
    stranger = client.post("/api/auth/register", json={
        "email": "stranger@elsewhere.example", "password": "pw-stranger-1", "name": "S", "handle": "stranger"})
    assert stranger.status_code in (200, 201), stranger.text
    stranger = {"Authorization": f"Bearer {stranger.json()['access_token']}"}
    assert _get(client, org, org["ps"][0], stranger).status_code in (403, 404)


def test_accept_carries_exceptions_and_skips_locked(client, org, monkeypatch):
    p = org["ps"][0]
    client.post("/api/rules/profiles/standard/apply", params={"project_id": p},
                json={"reason": "x", "except": [RULE]}, headers=org["owner"])
    _newer_pack(monkeypatch)
    r = client.post("/api/rules/profiles/standard/updates/accept", params={"project_id": p},
                    json={"reason": "go"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["excepted"] == [RULE]
    last = _events("apply_rule_profile")[-1].meta
    assert last["profile_version"] == "2" and last["exceptions"] == [RULE]


def test_a_project_reader_who_is_not_an_org_admin_reads_profiles_and_applied(client, org):
    p = org["ps"][0]
    client.post("/api/rules/profiles/standard/apply", params={"project_id": p},
                json={"reason": "x", "except": [OTHER]}, headers=org["owner"])
    inv = client.post(f"/api/orgs/{org['id']}/invites",
                      json={"email": "ops@ascme-labs.com", "role": "member"}, headers=org["owner"])
    assert inv.status_code in (200, 201), inv.text
    ops = _login(client, "ops@ascme-labs.com")
    acc = client.post("/api/invites/accept",
                      json={"token": inv.json()["accept_url"].rsplit("/", 1)[-1]}, headers=ops)
    assert acc.status_code == 200, acc.text
    rows = client.get(f"/api/orgs/{org['id']}/members", headers=org["owner"]).json()
    row = next(r for r in rows if r["user"]["email"] == "ops@ascme-labs.com")
    assert row["role"] == "member"  # not an org owner or admin
    g = client.put(f"/api/projects/{p}/members/{row['user']['id']}", json={"access": "read"},
                   headers=org["owner"])
    assert g.status_code == 200, g.text
    # The org-admin profile list stays closed to this person; the project read is open.
    from fastapi import HTTPException

    from app.models import User
    from tests.test_rule_toggles import _db

    db = _db()
    try:
        with pytest.raises(HTTPException) as e:
            RO.list_profiles(db, db.get(User, row["user"]["id"]), org["id"])
        assert e.value.status_code in (403, 404)
    finally:
        db.close()
    r = _get(client, org, p, ops)
    assert r.status_code == 200, r.text
    a = r.json()["applied"]
    assert (a["profile"], a["version"], a["exceptions"]) == ("standard", "1", [OTHER])
    assert [x["id"] for x in r.json()["profiles"]] == ["standard", "bad"]


def test_accept_keeps_exception_and_skips_lock_and_the_event_records_both(client, org, monkeypatch):
    """Except OTHER, org-lock RULE, take a v2 that still pins both, accept."""
    from app.services import rule_pack as P

    from tests.test_rules_org import _org_toggle

    p = org["ps"][0]
    r = client.post("/api/rules/profiles/standard/apply", params={"project_id": p},
                    json={"reason": "x", "except": [OTHER]}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert _org_toggle(org, RULE, {"on": False, "locked": True, "reason": "freeze"}).status_code == 200
    cur = P.CURRENT
    v2 = {"id": "standard", "title": "Standard", "version": "2",
          "rules": {RULE: {"state": "on", "params": {"n": 5}}, OTHER: {"state": "on"}}}
    monkeypatch.setattr(P, "CURRENT", P.Pack(
        cur.name, "1.1.0", cur.primitive_api, cur.min_server, {}, {},
        {**cur.profiles, "standard": v2}, cur.mappings))
    r = client.post("/api/rules/profiles/standard/updates/accept", params={"project_id": p},
                    json={"reason": "go"}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["excepted"] == [OTHER] and r.json()["skipped_locked"] == [RULE]
    meta = _events("apply_rule_profile")[-1].meta
    assert meta["profile_version"] == "2"
    assert meta["exceptions"] == [OTHER] and meta["skipped"] == [RULE]
    a = _get(client, org, p).json()["applied"]
    assert (a["version"], a["exceptions"], a["skipped"]) == ("2", [OTHER], [RULE])


def _pin_repo_rule(pin_rule, pin):
    from app.services import rule_pack as P
    P.CURRENT.profiles["repo"] = {"id": "repo", "version": "1", "rules": {RULE: {"state": "on"}, pin_rule: pin}}


@pytest.mark.parametrize("rule_id,pin", [
    ("build.generated_files", {"state": "on", "params": {"paths": ["x/**"]}}),
    ("plan.exclusive_areas", {"state": "on", "params": {"paths": ["a/"]}}),
    ("plan.path_classes", {"state": "on"}),
    ("build.test_scope", {"state": "on", "params": {"engines": ["sqlite"]}}),
])
def test_a_level_pinning_a_repo_specific_rule_is_refused_and_unusable(client, org, rule_id, pin):
    from app.services import rule_pack as P
    _pin_repo_rule(rule_id, pin)
    try:
        p = org["ps"][0]
        r = client.post("/api/rules/profiles/repo/apply", params={"project_id": p},
                        json={"reason": "x"}, headers=org["owner"])
        assert r.status_code == 422 and "repo-specific" in r.text and rule_id in r.text, r.text
        assert not _events("apply_rule_profile")
        listed = next(x for x in _get(client, org, p).json()["profiles"] if x["id"] == "repo")
        assert listed["usable"] is False and rule_id in listed["unusable_reason"]
    finally:
        P.CURRENT.profiles.pop("repo")
