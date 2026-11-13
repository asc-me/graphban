"""GRPH-1609: project-scoped read of the pack's profiles and the applied level; accept keeps exceptions."""
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
