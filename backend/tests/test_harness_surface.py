"""Skill and MCP names posted from a machine, and the difference the Harness page shows.

A list that was claimed complete and then omitted must not land as an empty list. Empty
is what a child with no loader looks like, and the page would call that a discrepancy.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Surface"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


def _post(client, key, proj, harnesses, host="laptop"):
    return client.post("/api/harness/surface",
                       json={"project_id": proj, "host": host, "harnesses": harnesses},
                       headers={"X-API-Key": key})


def test_an_omitted_complete_list_is_stored_as_unknown(client, auth, proj, key):
    r = _post(client, key, proj, [{
        "vendor": "grok",
        "installed": True,
        "skills_status": "checked",
        "mcps_status": "checked",
        "mcps": [{"name": "graphban", "source": "seat", "target": "https://example.test/token"}],
    }])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reported"] is True
    grok = next(row for row in body["harnesses"] if row["vendor"] == "grok")
    assert grok["skills_status"] == "unknown"
    assert grok["skills"] == []
    assert "omitted" in grok["skills_reason"]
    assert grok["mcps"] == [{"name": "graphban", "source": "seat"}]
    blob = r.text
    assert "token" not in blob
    assert "example.test" not in blob

    page = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    assert page["surface"]["skills"]["rows"] == []
    assert "not a discrepancy" in page["surface"]["skills"]["reason"]


def test_a_name_on_one_checked_harness_and_not_another_is_a_discrepancy(client, auth, proj, key):
    r = _post(client, key, proj, [
        {"vendor": "grok", "installed": True, "skills_status": "checked",
         "skills": [{"name": "commit", "source": "user"}], "disabled": [],
         "mcps_status": "checked",
         "mcps": [{"name": "graphban", "source": "seat"}, {"name": "context7", "source": "claudeJson"}]},
        {"vendor": "claude", "installed": True, "skills_status": "partial",
         "skills": [], "skills_reason": "directories only",
         "mcps_status": "checked", "mcps": [{"name": "graphban", "source": "seat"}]},
        {"vendor": "gbagent", "installed": True, "skills_status": "checked", "skills": [],
         "mcps_status": "checked", "mcps": [{"name": "graphban", "source": "seat"}]},
    ])
    assert r.status_code == 200, r.text
    skills = {row["name"]: row for row in r.json()["skills"]["rows"]}
    assert "claude" not in skills["commit"]["absent"]
    assert skills["commit"]["absent"] == ["gbagent"]
    mcps = {row["name"]: row for row in r.json()["mcps"]["rows"]}
    assert mcps["context7"]["present"] == ["grok"]
    assert "claude" in mcps["context7"]["absent"]
    assert "graphban" not in mcps


def test_no_report_is_not_an_agreement(client, auth, proj):
    page = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    assert page["surface"]["reported"] is False
    assert page["surface"]["skills"] is None
    assert "no machine has reported" in page["surface"]["reason"]


def test_a_key_that_cannot_write_the_project_is_told_nothing(client, proj):
    kate = client.post("/api/auth/login", json={"email": "kate@ascme-labs.com",
                                               "password": "graphban"}).json()["access_token"]
    kate_auth = {"Authorization": f"Bearer {kate}"}
    other = client.post("/api/projects", json={"name": "Elsewhere"}, headers=kate_auth).json()["id"]
    stranger = client.post("/api/api-keys", json={"name": "stranger", "project_id": other,
                                                  "scopes": ["read", "write"]},
                           headers=kate_auth).json()["plaintext"]
    r = _post(client, stranger, proj, [{
        "vendor": "grok", "skills_status": "checked", "skills": [],
        "mcps_status": "checked", "mcps": [],
    }])
    assert r.status_code == 404, r.text


def test_a_second_host_does_not_erase_the_first(client, auth, proj, key):
    assert _post(client, key, proj, [{
        "vendor": "grok", "skills_status": "checked", "skills": [{"name": "commit"}],
        "mcps_status": "checked", "mcps": [],
    }], host="laptop").status_code == 200
    page = _post(client, key, proj, [{
        "vendor": "grok", "skills_status": "checked", "skills": [{"name": "other"}],
        "mcps_status": "checked", "mcps": [],
    }], host="desk").json()
    assert page["host"] == "desk"
    assert [row["host"] for row in page["other_hosts"]] == ["laptop"]
