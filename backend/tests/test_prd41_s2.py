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


def _attempts_with_version(db, client, key, proj, auth, *, vendor, model, version, n, cap="B5"):
    """Finished attempts that declared a binary_version. Criterion 25's subject."""
    from datetime import datetime, timedelta, timezone

    from app.models import AttemptTelemetry

    T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    planner = _mcp(client, key, "register_agent", {"label": f"p-{version}"})["agent_id"]
    child = _mcp(client, key, "register_agent", {
        "label": f"c-{version}", "capabilities": {"vendor": vendor, "model": model},
    })["agent_id"]
    for i in range(n):
        item = client.post("/api/items", json={
            "title": f"{version} {i}", "project_id": proj,
            "touchpoints": ["web/src/features/x.tsx"],
        }, headers=auth).json()
        did = f"d-{version}-{i}"
        db.add(Delegation(
            id=did, project_id=proj, item_id=item["id"], delegated_by=planner,
            agent_id=child, linked_by="seat", lane="frontend", requested_tier="cheap",
            declared_model=model, declared_tier="local", outcome="signed_off",
            created_at=T0, claimed_at=T0 + timedelta(minutes=1),
            finished_at=T0 + timedelta(minutes=6), lease_seconds=600,
        ))
        db.add(AttemptTelemetry(
            id=f"at-{version}-{i}", delegation_id=did, project_id=proj, item_id=item["id"],
            vendor=vendor, model=model, binary_version=version, capabilities=[cap],
            outcome="signed_off", derived_at=T0,
        ))
    db.commit()


def test_unchanged_declared_triple_does_not_start_a_cell_or_suggest_a_probe(
        db, client, key, proj, auth):
    """25. A supervisor-side release that leaves vendor/model/binary_version unchanged
    starts no new cell and suggests no probe. Sabotage: keying the cell on a supervisor
    version (or pooling nothing) would split or invent a suggestion here."""
    _attempts_with_version(db, client, key, proj, auth, vendor="gbagent", model="q",
                           version="1.0.0", n=6)
    rows = dsvc.measured(db, proj)
    project = [c for c in rows if c["layer"] == "project" and c["capability"] == "B5"]
    assert len(project) == 1
    assert project[0]["binary_version"] == "1.0.0"
    assert project[0]["quality"]["n"] == 6
    suggestions = dsvc.probe_suggestions(db, proj)
    assert not any(s.get("trigger") == "version_change" for s in suggestions)
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert status["probe_suggestions"] == suggestions


def test_a_declared_version_change_starts_a_cell_and_inherits_the_prior(
        db, client, key, proj, auth):
    """25. A declared binary_version change starts a new cell, suggests a probe, and
    leaves the new cells filling from traffic with an inherited, labelled prior.
    Sabotage: pooling versions into one cell empties the prior and this fails."""
    _attempts_with_version(db, client, key, proj, auth, vendor="gbagent", model="q",
                           version="1.0.0", n=6)
    _attempts_with_version(db, client, key, proj, auth, vendor="gbagent", model="q",
                           version="2.0.0", n=1)
    rows = dsvc.measured(db, proj)
    project = [c for c in rows if c["layer"] == "project" and c["capability"] == "B5"]
    by_ver = {c.get("binary_version"): c for c in project}
    assert set(by_ver) == {"1.0.0", "2.0.0"}, by_ver
    assert by_ver["1.0.0"]["quality"]["n"] == 6
    assert by_ver["2.0.0"]["quality"]["n"] == 1
    prior = next(c for c in rows if c["layer"] == "prior" and c["capability"] == "B5")
    assert prior["binary_version"] == "2.0.0"
    assert prior["inherited_from"] == "1.0.0"
    assert prior["quality"]["n"] == 6
    suggestions = dsvc.probe_suggestions(db, proj)
    hit = next(s for s in suggestions if s["trigger"] == "version_change")
    assert hit["binary_version"] == "2.0.0"
    assert hit["inherited_from"] == "1.0.0"
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert any(s["trigger"] == "version_change" for s in status["probe_suggestions"])


def test_fleet_status_always_carries_probe_suggestions_even_when_empty(client, key, proj):
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert status["probe_suggestions"] == []
    assert status["measured"] == []


def test_brief_spend_includes_period_tokens_when_the_policy_names_a_period(
        client, key, proj, auth, db):
    from datetime import datetime, timezone

    from app.models import AttemptTelemetry, Project

    T0 = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    item = _mcp(client, key, "create_item", {
        "title": "period spend", "status": "next",
        "touchpoints": ["web/src/features/x.tsx"],
    })
    db.add(AttemptTelemetry(
        id="at-period-1", project_id=proj, item_id=item["id"],
        vendor="gbagent", model="q", capabilities=["B5"], outcome="signed_off",
        tokens_in=10_000, tokens_out=5_000, derived_at=T0,
    ))
    project = db.get(Project, proj)
    project.fleet_policy = {"local_only": False, "reviewer_cross_vendor": False,
                            "allowed_harnesses": [],
                            "caps": {"per_period_tokens": 100_000, "period": "week"}}
    db.commit()
    brief = _mcp(client, key, "get_item_details", {"id": item["id"]})["brief"]
    assert brief["spend"]["period_tokens"] == 15_000
    assert brief["spend"]["period_reported"] == 1
