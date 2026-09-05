"""PRD-37 PR 2 — profiles and policy: stored on the server, served in payloads the supervisor
already reads (criteria 3, 4). The server never resolves; these tests pin what it stores,
whose profile a payload carries, and that absence is spelled `null` rather than omitted.
"""
from __future__ import annotations

import pytest

from app.models import Project
from app.services import fleet_profiles


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Prefs"}, headers=auth).json()["id"]


@pytest.fixture()
def other(client, auth):
    return client.post("/api/projects", json={"name": "Other"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "sup", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


PROFILE = {"defaults": ["gbagent", "claude"], "weights": {"cost": 1.0, "quality": 0.25},
           "excludes": ["grok"]}


# ---- criterion 3: stored per user, per user-project, over REST ------------------------------

def test_a_default_profile_is_written_and_read_back_over_rest(client, auth):
    put = client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    assert put.status_code == 200, put.text
    assert put.json()["scope"] == "default"
    got = client.get("/api/fleet/profile", headers=auth).json()
    assert got["default"]["defaults"] == ["gbagent", "claude"]
    assert got["default"]["weights"] == {"cost": 1.0, "quality": 0.25}
    assert got["default"]["excludes"] == ["grok"]
    assert got["override"] is None
    assert got["profile"]["scope"] == "default"


def test_a_project_override_wins_over_the_default_there_and_nowhere_else(client, auth, proj, other):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    over = client.put("/api/fleet/profile", json={**PROFILE, "project_id": proj,
                                                  "defaults": ["claude"]}, headers=auth)
    assert over.status_code == 200, over.text
    here = client.get(f"/api/fleet/profile?project_id={proj}", headers=auth).json()
    assert here["profile"]["scope"] == "project" and here["profile"]["defaults"] == ["claude"]
    assert here["default"]["defaults"] == ["gbagent", "claude"], "the default is still shown beside it"
    there = client.get(f"/api/fleet/profile?project_id={other}", headers=auth).json()
    assert there["profile"]["scope"] == "default" and there["override"] is None


def test_writing_again_updates_the_one_row_rather_than_adding_a_second(client, auth, db):
    from sqlalchemy import select
    from app.models import FleetProfile

    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/profile", json={**PROFILE, "defaults": ["claude"]}, headers=auth)
    rows = db.scalars(select(FleetProfile)).all()
    assert len(rows) == 1 and rows[0].defaults == ["claude"]


def test_clearing_an_override_falls_back_to_the_default(client, auth, proj):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/profile", json={**PROFILE, "project_id": proj, "defaults": ["claude"]}, headers=auth)
    assert client.delete(f"/api/fleet/profile?project_id={proj}", headers=auth).json() == {"cleared": True}
    assert client.delete(f"/api/fleet/profile?project_id={proj}", headers=auth).json() == {"cleared": False}
    got = client.get(f"/api/fleet/profile?project_id={proj}", headers=auth).json()
    assert got["profile"]["scope"] == "default"


@pytest.mark.parametrize("bad, msg", [
    ({"weights": {"speed": 1.0}}, "unknown weight axis"),
    ({"weights": {"cost": 1.5}}, "between 0 and 1"),
    ({"defaults": ["claude", "claude"]}, "twice"),
    ({"defaults": ["claude"], "excludes": ["claude"]}, "both in defaults and excludes"),
    ({"defaults": [""]}, "non-empty"),
])
def test_a_profile_that_says_something_it_may_not_is_refused_naming_the_field(client, auth, bad, msg):
    r = client.put("/api/fleet/profile", json={**PROFILE, **bad}, headers=auth)
    assert r.status_code == 422, r.text
    assert msg in r.json()["detail"]


def test_a_policy_is_stored_on_the_project_and_read_back(client, auth, proj, db):
    put = client.put("/api/fleet/policy", json={"project_id": proj, "local_only": True,
                                                "allowed_harnesses": ["gbagent"]}, headers=auth)
    assert put.status_code == 200, put.text
    assert put.json()["policy"] == {"local_only": True, "reviewer_cross_vendor": False,
                                    "allowed_harnesses": ["gbagent"]}
    assert db.get(Project, proj).fleet_policy["local_only"] is True
    got = client.get(f"/api/fleet/policy?project_id={proj}", headers=auth).json()
    assert got["policy"]["allowed_harnesses"] == ["gbagent"]


def test_a_policy_with_every_constraint_off_is_stored_as_null_not_as_an_empty_rule(client, auth, proj, db):
    client.put("/api/fleet/policy", json={"project_id": proj, "local_only": True}, headers=auth)
    r = client.put("/api/fleet/policy", json={"project_id": proj}, headers=auth)
    assert r.json()["policy"] is None
    db.expire_all()
    assert db.get(Project, proj).fleet_policy is None


def test_policy_refuses_a_key_it_does_not_know_rather_than_storing_it_silently():
    with pytest.raises(fleet_profiles.ProfileInvalid, match="local_onyl"):
        fleet_profiles.normalise_policy({"local_onyl": True})


def test_policy_takes_the_projects_write_gate(client, auth, proj, monkeypatch):
    from app.security import authz

    # A member who can read but not write: the same gate every other project setting takes.
    monkeypatch.setattr(authz, "can_write", lambda db, user_id, project_id: False)
    r = client.put("/api/fleet/policy", json={"project_id": proj, "local_only": True}, headers=auth)
    assert r.status_code == 403, r.text


# ---- criterion 4: the payloads --------------------------------------------------------------

def test_fleet_status_carries_the_key_owners_profile_and_the_projects_policy(client, auth, proj, key):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/policy", json={"project_id": proj, "local_only": True}, headers=auth)
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert status["profile"]["defaults"] == ["gbagent", "claude"]
    assert status["profile"]["weights"] == {"cost": 1.0, "quality": 0.25}
    assert status["profile"]["user"], "the explanation needs a name for whose taste this was"
    assert status["policy"] == {"local_only": True, "reviewer_cross_vendor": False, "allowed_harnesses": []}


def test_fleet_status_prefers_the_project_override_for_that_project(client, auth, proj, key):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/profile", json={**PROFILE, "project_id": proj, "defaults": ["claude"]}, headers=auth)
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert status["profile"]["defaults"] == ["claude"] and status["profile"]["scope"] == "project"


def test_no_profile_and_no_policy_are_spelled_null_never_omitted(client, key, proj):
    """Absence must not read as clean: a supervisor that finds no key at all cannot tell a
    dropped field from an empty preference. Both keys are present, both null."""
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert "profile" in status and status["profile"] is None
    assert "policy" in status and status["policy"] is None


def test_the_brief_carries_the_same_profile_and_policy_and_its_text_does_not(client, auth, proj, key):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/policy", json={"project_id": proj, "allowed_harnesses": ["gbagent"]}, headers=auth)
    item = client.post("/api/items", json={"title": "Prefs item", "project_id": proj,
                                           "touchpoints": ["backend/app/x.py"]}, headers=auth).json()
    details = _mcp(client, key, "get_item_details", {"id": item["id"]})
    brief = details["brief"]
    assert brief["profile"]["defaults"] == ["gbagent", "claude"]
    assert brief["policy"]["allowed_harnesses"] == ["gbagent"]
    assert "gbagent" not in brief["text"], "the spawn text carries no suggestion (PRD-35 D5)"


def test_the_fleet_view_read_carries_them_too(client, auth, proj):
    client.put("/api/fleet/profile", json=PROFILE, headers=auth)
    client.put("/api/fleet/policy", json={"project_id": proj, "local_only": True}, headers=auth)
    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()
    assert view["profile"]["defaults"] == ["gbagent", "claude"]
    assert view["policy"]["local_only"] is True
