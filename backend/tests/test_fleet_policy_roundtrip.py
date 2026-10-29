"""GRPH-1161 — PUT /api/fleet/policy is a full replace, so a read-modify-write round trip must
keep every key, `cluster_ceiling` included."""
from __future__ import annotations

import pytest

FULL = {
    "local_only": True,
    "reviewer_cross_vendor": True,
    "allowed_harnesses": ["gbagent", "claude"],
    "caps": {"per_attempt_tokens": 1000, "per_item_tokens": 2000,
             "per_period_tokens": 3000, "period": "week"},
    "cluster_ceiling": {"frontend": 2, "backend": 5, "mixed": 3},
}


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Roundtrip"}, headers=auth).json()["id"]


def test_put_back_with_one_cap_changed_keeps_every_other_setting(client, auth, proj):
    r = client.put("/api/fleet/policy", json={"project_id": proj, **FULL}, headers=auth)
    assert r.status_code == 200, r.text
    got = client.get(f"/api/fleet/policy?project_id={proj}", headers=auth).json()["policy"]
    assert got == FULL

    edited = {**got, "caps": {**got["caps"], "per_item_tokens": 9999}}
    r = client.put("/api/fleet/policy", json={"project_id": proj, **edited}, headers=auth)
    assert r.status_code == 200, r.text

    after = client.get(f"/api/fleet/policy?project_id={proj}", headers=auth).json()["policy"]
    assert after["caps"]["per_item_tokens"] == 9999
    assert after == {**FULL, "caps": {**FULL["caps"], "per_item_tokens": 9999}}


def test_an_invalid_ceiling_still_surfaces_the_profile_invalid_message(client, auth, proj):
    r = client.put("/api/fleet/policy",
                   json={"project_id": proj, "cluster_ceiling": {"sideways": 2}}, headers=auth)
    assert r.status_code == 422, r.text
    assert "unknown cluster_ceiling lane 'sideways'" in r.json()["detail"]
