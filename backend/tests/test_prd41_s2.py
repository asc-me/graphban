"""PRD-41 S2 — capabilities at delegate, the brief bound, budget/caps, measured re-key.

Criteria 4, 14 (the server half), 15's bound, D20 REST.
"""
from __future__ import annotations

import json

import pytest

from sqlalchemy import select

from app.models import AttemptTelemetry, Delegation, Project
from app.services import delegation as dsvc
from app.services import fleet_profiles
from app.services import items as items_svc


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
    return client.post("/api/projects", json={"name": "S2"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "s2", "project_id": proj,
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


def test_delegate_on_a_web_feature_records_B5_and_the_brief_carries_it(client, key, db):
    """4. Touchpoints under web/src/features → capabilities_at_delegate = [B5]."""
    planner = _mcp(client, key, "register_agent", {"label": "planner-s2"})["agent_id"]
    item = _mcp(client, key, "create_item", {
        "title": "a react feature", "status": "next",
        "touchpoints": ["web/src/features/fleet/FleetView.tsx"],
    })["id"]
    brief = _mcp(client, key, "get_item_details", {"id": item})["brief"]
    assert brief["capabilities"] == ["B5"]
    payload = json.dumps({"capabilities": brief["capabilities"],
                          "measured_for_lane": brief["measured_for_lane"]},
                         separators=(",", ":"))
    assert len(payload) <= dsvc.BRIEF_MEASURED_BOUND
    d = _mcp(client, key, "delegate", {"id": item, "lane": "frontend", "tier": "cheap",
                                       "agent_id": planner})
    assert d["brief"]["capabilities"] == ["B5"]
    row = db.get(Delegation, d["delegation_id"])
    assert row.capabilities_at_delegate == ["B5"]


def test_exit_time_set_may_be_larger_and_both_are_on_the_row(client, key, db):
    from datetime import datetime, timezone

    from app.services import harness as harness_svc

    planner = _mcp(client, key, "register_agent", {"label": "planner-s2b"})["agent_id"]
    item = _mcp(client, key, "create_item", {
        "title": "feature plus route", "status": "next",
        "touchpoints": ["web/src/features/x/X.tsx"],
    })["id"]
    d = _mcp(client, key, "delegate", {"id": item, "lane": "frontend", "tier": "cheap",
                                       "agent_id": planner})
    child = _mcp(client, key, "register_agent", {
        "label": "child-s2b", "parent_agent_id": planner,
        "capabilities": {"vendor": "gbagent", "model": "q"},
    })["agent_id"]
    assert items_svc.claim_item(db, item, child) is not None
    row = db.get(Delegation, d["delegation_id"])
    row.outcome = "signed_off"
    row.finished_at = datetime.now(timezone.utc)
    db.flush()
    tel = harness_svc.derive(db, row)
    db.commit()
    assert tel is not None
    assert tel.capabilities_at_delegate == ["B5"]
    assert "B5" in (tel.capabilities or [])


def test_measured_rekeys_on_capability_with_a_layer_and_bands(db, client, key, proj, auth):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select

    T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    planner = _mcp(client, key, "register_agent", {"label": "p-meas"})["agent_id"]
    child = _mcp(client, key, "register_agent", {
        "label": "c-meas", "capabilities": {"vendor": "gbagent", "model": "q"},
    })["agent_id"]
    item = client.post("/api/items", json={
        "title": "web work", "project_id": proj,
        "touchpoints": ["web/src/features/a.tsx"],
    }, headers=auth).json()
    db.add(Delegation(
        id=f"d-{item['id']}", project_id=proj, item_id=item["id"], delegated_by=planner,
        agent_id=child, linked_by="seat", lane="frontend", requested_tier="cheap",
        declared_model="q", declared_tier="local", outcome="signed_off",
        created_at=T0, claimed_at=T0 + timedelta(minutes=1),
        finished_at=T0 + timedelta(minutes=6), lease_seconds=600,
        capabilities_at_delegate=["B5"],
    ))
    db.commit()
    rows = dsvc.measured(db, proj)
    assert rows
    cell = rows[0]
    assert cell["capability"] == "B5"
    assert cell["layer"] == "project"
    assert "bands" in cell
    assert cell["quality"]["n"] == 1
    assert "lane" not in cell and "tier" not in cell


def test_budget_tokens_and_caps_round_trip_over_rest(client, auth, proj, db):
    put = client.put("/api/fleet/profile", json={
        "defaults": ["gbagent"], "weights": {"cost": 1.0}, "excludes": [],
        "budget_tokens": 50_000,
    }, headers=auth)
    assert put.status_code == 200, put.text
    assert put.json()["budget_tokens"] == 50_000
    got = client.get("/api/fleet/profile", headers=auth).json()
    assert got["profile"]["budget_tokens"] == 50_000

    pol = client.put("/api/fleet/policy", json={
        "project_id": proj, "local_only": False,
        "caps": {"per_item_tokens": 120_000, "per_attempt_tokens": 80_000},
    }, headers=auth)
    assert pol.status_code == 200, pol.text
    assert pol.json()["policy"]["caps"]["per_item_tokens"] == 120_000
    assert db.get(Project, proj).fleet_policy["caps"]["per_item_tokens"] == 120_000


def test_caps_unknown_key_is_refused():
    with pytest.raises(fleet_profiles.ProfileInvalid, match="unknown cap key"):
        fleet_profiles.normalise_policy({"caps": {"per_day": 1}})


def test_tokens_to_signoff_counts_bounced_attempts_in_the_numerator(db, client, key, proj, auth):
    """16 sabotage: counting only signed-off tokens makes a 30% row look cheap."""
    from datetime import datetime, timedelta, timezone

    from app.models import AttemptTelemetry

    T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    planner = _mcp(client, key, "register_agent", {"label": "p-cost"})["agent_id"]
    child = _mcp(client, key, "register_agent", {
        "label": "c-cost", "capabilities": {"vendor": "gbagent", "model": "q"},
    })["agent_id"]
    for i, outcome in enumerate(["signed_off"] * 3 + ["bounced"] * 7):
        item = client.post("/api/items", json={
            "title": f"cost {i}", "project_id": proj,
            "touchpoints": ["web/src/features/x.tsx"],
        }, headers=auth).json()
        did = f"d-cost-{i}"
        db.add(Delegation(
            id=did, project_id=proj, item_id=item["id"], delegated_by=planner,
            agent_id=child, linked_by="seat", lane="frontend", requested_tier="cheap",
            declared_model="q", declared_tier="local", outcome=outcome,
            created_at=T0, claimed_at=T0 + timedelta(minutes=1),
            finished_at=T0 + timedelta(minutes=6), lease_seconds=600,
        ))
        db.add(AttemptTelemetry(
            id=f"at-cost-{i}", delegation_id=did, project_id=proj, item_id=item["id"],
            vendor="gbagent", model="q", capabilities=["B5"], outcome=outcome,
            tokens_in=5000, tokens_out=5000, derived_at=T0,
        ))
    db.commit()
    cell = next(c for c in dsvc.measured(db, proj) if c["capability"] == "B5")
    # 10 × 10k tokens / 3 signed-off = 33333. Signed-off-only would be 10000.
    assert cell["cost"]["comparable"] is True
    assert cell["cost"]["tokens_to_signoff"] > 20_000


def test_budget_tokens_must_be_a_positive_integer(client, auth):
    r = client.put("/api/fleet/profile", json={"budget_tokens": 0}, headers=auth)
    assert r.status_code == 422
    r = client.put("/api/fleet/profile", json={"budget_tokens": -5}, headers=auth)
    assert r.status_code == 422
