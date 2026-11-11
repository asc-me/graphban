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
    assert len(body["grading_rules"]) == 7
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


def _cell(vendor, cap, bands, tokens=None):
    cost = ({"comparable": True, "tokens_to_signoff": tokens} if tokens is not None
            else {"comparable": False, "reason": "not comparable"})
    return {"vendor": vendor, "model": "m", "capability": cap, "layer": "project",
            "bands": {b: {"value": v, "n": n} for b, (v, n) in bands.items()}, "cost": cost}


def _row(rows, band, cap="B5"):
    return next(r for r in rows if r["effort_band"] == band and r["function"] == cap)


def test_routing_grades_prefer_ok_avoid_with_fallback():
    rows = hsvc._routing_from_measured([
        _cell("a", "B5", {"S": (0.9, 10), "M": (0.7, 10), "L": (0.5, 10)}),
        _cell("b", "B5", {"S": (0.6, 12), "M": (0.6, 12), "L": (0.3, 12)}),
    ], floor=5)
    s, m, l = _row(rows, "S"), _row(rows, "M"), _row(rows, "L")
    assert (s["verdict"], s["pick"], s["fallback"]) == ("PREFER", "a:m", "b:m")
    assert s["evidence"] == "90% · 9/10" and s["confidence"] == "high"
    assert s["label"] == hsvc.CAPABILITY_LABELS["B5"]
    assert m["verdict"] == "OK" and l["verdict"] == "AVOID"
    assert l["fallback_rate"] == 0.3


def test_routing_above_floor_outranks_a_higher_below_floor_rate():
    rows = hsvc._routing_from_measured([
        _cell("thin", "B5", {"M": (1.0, 1)}), _cell("real", "B5", {"M": (0.9, 50)})], floor=5)
    m = _row(rows, "M")
    assert m["pick"] == "real:m" and m["verdict"] == "PREFER"
    # A below-floor row is never cited as the fallback.
    assert m["fallback"] is None


def test_routing_probe_when_nothing_clears_the_floor():
    rows = hsvc._routing_from_measured([
        _cell("a", "B5", {"M": (1.0, 2)}), _cell("b", "B5", {"M": (0.5, 4)})], floor=5)
    m = _row(rows, "M")
    assert m["verdict"] == "PROBE" and m["pick"] == "b:m" and m["rate"] is None
    assert m["confidence"] == "low" and "2/4" in m["evidence"]


def test_routing_cheaper_pick_within_margin_takes_the_cell():
    cells = [_cell("dear", "B5", {"M": (0.92, 20)}, tokens=60000),
             _cell("cheap", "B5", {"M": (0.90, 20)}, tokens=15000)]
    m = _row(hsvc._routing_from_measured(cells, floor=5), "M")
    assert (m["pick"], m["verdict"], m["cheaper"]) == ("cheap:m", "OK", True)
    assert m["fallback"] == "dear:m"
    # Outside the margin, or with an incomparable cost, the best rate keeps it.
    cells[1]["bands"]["M"]["value"] = 0.80
    assert _row(hsvc._routing_from_measured(cells, floor=5), "M")["pick"] == "dear:m"
    cells[1]["bands"]["M"]["value"] = 0.90
    cells[1]["cost"] = {"comparable": False}
    assert _row(hsvc._routing_from_measured(cells, floor=5), "M")["pick"] == "dear:m"


def test_guidance_text_is_the_string_fleet_status_carries(client, auth, proj):
    """The page shows what fleet_status serves — read back, not re-rendered."""
    from app.db import SessionLocal
    from app.services import fleet as fleet_svc
    body = client.get(f"/api/harness/guidance?project_id={proj}", headers=auth).json()
    assert body["served_text"].startswith("# gbfleet routing guidance")
    assert [v["id"] for v in body["verdict_rules"]] == list(hsvc.VERDICT_ORDER)
    with SessionLocal() as db:
        served = fleet_svc.fleet_status(db, proj)["routing_guidance"]
    assert served.splitlines()[0] == body["served_text"].splitlines()[0]
    assert json.loads(body["fleet_status_text"])["routing_guidance"] == body["served_text"]


def test_routing_text_lists_each_band_and_flags_guesses():
    rows = hsvc._routing_from_measured([
        _cell("a", "B5", {"S": (0.9, 10), "M": (1.0, 2)})], floor=5)
    text = hsvc.routing_guidance_text(rows, project_tag="GRPH", window_days=90, floor=5,
                                      generated_at="2026-11-10T00:00Z")
    assert "[B5 " in text
    assert "  S  PREFER a:m" in text
    assert "  M  PROBE  a:m" in text and "# guess — flag the spawn" in text
    assert "#  PREFER" in text


def test_guidance_rules_come_from_server_not_client_copy(client, auth, proj):
    """CALL sabotage: if the handler stopped calling rule_catalog, thresholds would drift."""
    r = client.get(f"/api/harness/guidance?project_id={proj}", headers=auth)
    r2 = next(x for x in r.json()["grading_rules"] if x["id"] == "R2")
    assert r2["thresholds"]["max_rate"] == harness_rules.DEMOTE_MAX_RATE
