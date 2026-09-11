"""GRPH-865 — mix on the profile, mix_counts on the brief.

The resolver lives in the fleet package; this file pins what the server stores and
what the brief hands the supervisor. n=0 is a present empty histogram, not an absent key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import AttemptTelemetry
from app.services import delegation as dsvc
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
    return client.post("/api/projects", json={"name": "Mix"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "mix", "project_id": proj,
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


def test_mix_round_trips_over_rest_and_empty_object_stores_null(client, auth):
    put = client.put("/api/fleet/profile", json={
        "defaults": ["claude", "grok"], "weights": {}, "excludes": [],
        "mix": {"claude": 0.4, "grok": 0.4},
    }, headers=auth)
    assert put.status_code == 200, put.text
    mix = put.json()["mix"]
    assert mix is not None
    assert abs(mix["claude"] - 0.5) < 1e-9
    assert abs(mix["grok"] - 0.5) < 1e-9
    got = client.get("/api/fleet/profile", headers=auth).json()
    assert got["profile"]["mix"]["claude"] == pytest.approx(0.5)

    cleared = client.put("/api/fleet/profile", json={
        "defaults": ["claude", "grok"], "weights": {}, "excludes": [], "mix": {},
    }, headers=auth)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mix"] is None


@pytest.mark.parametrize("bad, msg", [
    ({"mix": {"gbagent": 0, "claude": 0}, "defaults": ["gbagent", "claude"]}, "share > 0"),
    ({"mix": {"grok": 1}, "defaults": ["gbagent", "claude"], "excludes": ["grok"]},
     "mix and excludes"),
    ({"mix": {"cursor-agent": 1}, "defaults": ["gbagent", "claude"]}, "not in defaults"),
])
def test_mix_that_says_something_it_may_not_is_refused(client, auth, bad, msg):
    r = client.put("/api/fleet/profile", json={"defaults": bad.get("defaults", ["gbagent"]),
                                               "weights": {}, "excludes": bad.get("excludes", []),
                                               "mix": bad["mix"]}, headers=auth)
    assert r.status_code == 422, r.text
    assert msg in r.json()["detail"]


def test_mix_must_be_an_object():
    with pytest.raises(fleet_profiles.ProfileInvalid, match="object"):
        fleet_profiles._mix("claude", defaults=[], excludes=[])


def test_mix_counts_last_window_matrix_only_and_unreported_is_not_zero_share(db, proj):
    T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    for i, (source, harness) in enumerate([
        ("matrix", "claude"),
        ("matrix", "claude"),
        ("flag", "grok"),
        ("matrix", "gbagent"),
        (None, "claude"),
    ]):
        db.add(AttemptTelemetry(
            id=f"at-mix-{i}", project_id=proj,
            chosen_source=source, adapter_launched=harness if source == "matrix" else None,
            chosen_winner=f"{harness}:x" if source else None,
            reported_at=T0 + timedelta(minutes=i),
        ))
    db.commit()
    got = dsvc.mix_counts(db, proj)
    assert got["n"] == 3
    assert got["by_harness"] == {"claude": 2, "gbagent": 1}
    assert got["unreported"] == 2
    assert "grok" not in got["by_harness"]


def test_brief_mix_is_present_at_n_zero_never_omitted(client, key, proj):
    item = _mcp(client, key, "create_item", {
        "title": "mix brief", "status": "next",
        "touchpoints": ["backend/app/x.py"],
    })
    brief = _mcp(client, key, "get_item_details", {"id": item["id"]})["brief"]
    assert brief["mix"] == {"n": 0, "by_harness": {}, "unreported": 0}


def test_mix_counts_without_a_project_is_unmeasured_not_everyone_at_zero(db):
    assert dsvc.mix_counts(db, None) == {"n": 0, "by_harness": {}, "unreported": 0}
    assert fleet_profiles._mix({}, defaults=[], excludes=[]) is None
