"""PRD-47 S12 — Harness guidance: server-served rules, routing, and fleet_status text."""
from __future__ import annotations

import json

import pytest

from app.services import harness as hsvc
from app.services import harness_rules


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Guidance"}, headers=auth).json()["id"]


def test_rule_catalog_matches_harness_rules_constants():
    rules = harness_rules.rule_catalog()
    assert [r["id"] for r in rules] == list(harness_rules.RULES)
    r2 = next(r for r in rules if r["id"] == "R2")
    assert r2["thresholds"]["max_rate"] == harness_rules.DEMOTE_MAX_RATE
    assert r2["thresholds"]["min_finished"] == harness_rules.DEMOTE_MIN_FINISHED
    r5 = next(r for r in rules if r["id"] == "R5")
    assert r5["title"] == "Reprior"
    r6 = next(r for r in rules if r["id"] == "R6")
    assert r6["title"] == "Install"


def test_guidance_endpoint_returns_rules_routing_and_fleet_status(client, auth, proj):
    r = client.get(f"/api/harness/guidance?project_id={proj}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == proj
    assert len(body["grading_rules"]) == 6
    assert body["grading_rules"][1]["thresholds"]["max_rate"] == 0.25
    assert "routing" in body
    assert "generation_stamp" in body
    stamp = body["generation_stamp"]
    assert stamp["window_days"] == hsvc.WINDOW_DAYS
    assert stamp["floor"] == hsvc.FLOOR
    assert "attempts" in stamp
    assert "supervisors_served" in stamp
    parsed = json.loads(body["fleet_status_text"])
    assert "measured" in parsed
    assert "agents" in parsed
    assert "review_queue" not in parsed


def test_routing_from_measured_picks_best_rate_per_band():
    measured = [{
        "vendor": "gbagent", "model": "qwen3.6", "capability": "B5", "layer": "project",
        "bands": {"M": {"value": 0.8, "n": 10}, "L": {"value": 0.5, "n": 8}},
    }, {
        "vendor": "cursor", "model": "composer", "capability": "B5", "layer": "project",
        "bands": {"M": {"value": 0.6, "n": 12}},
    }]
    rows = hsvc._routing_from_measured(measured, floor=5)
    m_row = next(r for r in rows if r["effort_band"] == "M")
    assert m_row["pick"] == "gbagent:qwen3.6"
    assert m_row["fallback"] == "cursor:composer"
    assert m_row["verdict"] == "measured"


def test_routing_prefers_above_floor_over_higher_below_floor_rate():
    measured = [{
        "vendor": "unmeasured", "model": "model", "capability": "B5", "layer": "project",
        "bands": {"M": {"value": 1.0, "n": 1}},
    }, {
        "vendor": "measured", "model": "model", "capability": "B5", "layer": "project",
        "bands": {"M": {"value": 0.9, "n": 50}},
    }]
    rows = hsvc._routing_from_measured(measured, floor=5)
    m_row = next(r for r in rows if r["effort_band"] == "M")
    assert m_row["pick"] == "measured:model"
    assert m_row["verdict"] == "measured"
    assert m_row["fallback"] == "unmeasured:model"


def test_guidance_rules_come_from_server_not_client_copy(client, auth, proj):
    """CALL sabotage: if the handler stopped calling rule_catalog, thresholds would drift."""
    r = client.get(f"/api/harness/guidance?project_id={proj}", headers=auth)
    r2 = next(x for x in r.json()["grading_rules"] if x["id"] == "R2")
    assert r2["thresholds"]["max_rate"] == harness_rules.DEMOTE_MAX_RATE
