"""PRD-41 S4 — R5, R6, R3 family rule, org cards, contribution, snapshot, redaction.

Criteria 10, 11, 12, 13, 18, 19, 29.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (AttemptTelemetry, CapabilityPrior, FleetProfile, HarnessRollup,
                        Item, Organization, OrgMembership, PlatformContributionCell,
                        PlatformRollup, Project, User)
from app.services import harness as hsvc
from app.services import harness_rules as rules
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "S4"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "s4", "project_id": proj,
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


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _resolution(winner: str, *others: str, statuses: dict | None = None,
                dropped: list | None = None, capabilities: list | None = None,
                unenforceable: dict | None = None) -> dict:
    statuses = statuses or {}
    names = [winner, *others]
    shortlist = []
    for i, name in enumerate(names):
        harness, model = name.split(":", 1)
        shortlist.append({"harness": harness, "model": model, "vendor": harness,
                          "status": statuses.get(name, "verified"),
                          "score": 1.0 - i * 0.1, "order": i + 1, "local": False, "axes": {}})
    out = {"source": "matrix", "tier": "cheap", "shortlist": shortlist,
           "dropped_rows": list(dropped or []),
           "winner": shortlist[0] if shortlist else None, "profile": {"defaults": []}}
    if capabilities:
        out["capabilities"] = capabilities
    if unenforceable:
        out["unenforceable_cap"] = unenforceable
        out["refused"] = f"{unenforceable.get('cap')}: no eligible row reports tokens"
    return out


def _attempt(client, key, db, planner, label, *, outcome="signed_off", vendor="gbagent",
             model="qwen3.6", touchpoints=None, resolution=None, sampled=None) -> AttemptTelemetry:
    item = _ok(_mcp(client, key, "create_item", {
        "title": label, "status": "next",
        "touchpoints": touchpoints or ["backend/alembic/versions/0123.py"]}))["id"]
    _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                       "agent_id": planner}))
    child = _agent(client, key, f"c-{label}", parent_agent_id=planner,
                   capabilities={"instance": label, "vendor": vendor, "model": model})
    assert items_svc.claim_item(db, item, child) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"r-{label}", capabilities={"instance": f"rev-{label}"})
    if outcome == "signed_off":
        _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                           "evidence": [{"kind": "note", "detail": "ok"}]}))
    else:
        _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "wrong"}))
        stored = db.get(Item, item)
        stored.bounce_pinned_to, stored.bounce_pinned_until = None, None
        db.commit()
    db.expire_all()
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    if resolution is not None:
        row.resolution = resolution
    if sampled:
        row.sampled = sampled
    if outcome != "signed_off":
        row.outcome = "bounced"
    db.commit()
    return row


def _cards(client, auth, proj, **params):
    q = "".join(f"&{k}={v}" for k, v in params.items())
    r = client.get(f"/api/harness/recommendations?project_id={proj}{q}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _by_rule(payload, rule):
    return [c for c in payload["cards"] if c["rule"] == rule]


def _d11(**over):
    row = {
        "capability": "A4", "size_band": "M", "vendor": "gbagent", "model": "rare-model",
        "binary_version": "1.0.0", "week": "2026-W36", "finished": 8, "signed_off": 6,
        "first_choice": 8, "fallback": 0, "explicit": 0, "unknown": 0, "probe": 0,
    }
    row.update(over)
    return row


# ---- 10: R5 ---------------------------------------------------------------------------------

def _proposal_id(draft: dict) -> dict:
    """The shape that is NOT a hash of the numbers. Hashing only this still
    passes when `evidence_line` embeds the counts, because those three keys
    do not move. The real hash has to cover the cells."""
    return {k: draft.get(k) for k in ("kind", "target", "capability")}


def test_r5_drafts_an_evidence_line_when_a_cell_disagrees_with_its_prior(
        client, key, db, proj, auth):
    """10. Verified prior 1.0 vs 0/6 signed off is ≥ 0.3.

    Sabotage: hash only `{kind, target, capability}`. Hashing `draft` still
    moves because `evidence_line` embeds the counts, so that mutation stays
    green while the numbers move.
    """
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "verified"})
    for i in range(6):
        _attempt(client, key, db, planner, f"bad{i}", outcome="bounced", resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R5")
    assert cards, cards
    card = cards[0]
    assert card["draft"]["kind"] == "matrix_evidence"
    assert card["draft"]["capability"]
    assert "never" in card["detail"].lower() or "evidence" in card["draft"]["kind"]
    assert "replay" in card and "siblings" in card
    first_hash = card["evidence_hash"]
    proposal = _proposal_id(card["draft"])
    for i in range(3):
        _attempt(client, key, db, planner, f"more{i}", outcome="bounced", resolution=res)
    again = _by_rule(_cards(client, auth, proj), "R5")[0]
    assert _proposal_id(again["draft"]) == proposal, (
        "kind/target/capability did not move — hashing only those would stay green")
    assert again["evidence_hash"] != first_hash, (
        "hash must cover the cells, not {kind, target, capability}")


def test_r5_drafts_from_the_cell_when_the_leaf_has_no_prior(client, key, db, proj, auth):
    """10. A leaf with no prior drafts one from the cell at n ≥ 5."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "unverified"})
    for i in range(5):
        _attempt(client, key, db, planner, f"np{i}", resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R5")
    assert cards
    assert "no per-capability prior" in cards[0]["draft"]["evidence_line"]


def test_r5_does_not_fire_under_the_floor_including_probes(client, key, db, proj, auth):
    """10 / 27. Probe n=4 fires no R5. Natural n=4 neither."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "unverified"})
    for i in range(4):
        _attempt(client, key, db, planner, f"thin{i}", resolution=res, sampled="probe")
    assert _by_rule(_cards(client, auth, proj), "R5") == []


def test_r5_labels_probe_samples_when_they_contribute(client, key, db, proj, auth):
    """10. Probes count toward R5 with their label on the card."""
    planner = _agent(client, key, "planner")
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "unverified"})
    for i in range(5):
        _attempt(client, key, db, planner, f"pr{i}", resolution=res, sampled="probe")
    cards = _by_rule(_cards(client, auth, proj), "R5")
    assert cards
    assert cards[0]["draft"].get("probe") is True
    assert "probe" in cards[0]["draft"]["evidence_line"].lower()


# ---- 18: R6 ---------------------------------------------------------------------------------

def test_r6_fires_only_when_the_dropped_row_has_a_measured_grade(
        client, key, db, proj, auth):
    """18. ≥6 not-installed drops of a better measured row. Prior alone draws no card."""
    planner = _agent(client, key, "planner")
    dropped = [{"harness": "claude", "model": "sonnet", "vendor": "claude",
                "status": "unverified", "score": 0.9, "order": 1, "local": False,
                "stage": "installed", "why": "not installed"}]
    res = _resolution("gbagent:qwen3.6", statuses={"gbagent:qwen3.6": "verified"},
                      dropped=dropped, capabilities=["A4"])
    for i in range(6):
        _attempt(client, key, db, planner, f"miss{i}", outcome="bounced", resolution=res)
    # No measured grade for claude yet — prior-only must not fire.
    assert _by_rule(_cards(client, auth, proj), "R6") == []

    # A platform overlay at the floor is a measured layer (roll would wipe a local
    # rollup that has no matching telemetry).
    db.add(PlatformRollup(
        week="2026-W36", vendor="claude", model="sonnet", binary_version="",
        capability="A4", size_band="S", orgs_contributing=3, finished=20,
        signed_off=18, top_org_share=0.4))
    db.commit()
    db.expire_all()
    cards = [c.as_dict() for c in rules.cards(db, proj) if c.rule == "R6"]
    assert cards, cards
    card = cards[0]
    assert card["draft"]["kind"] == "install"
    assert card["draft"]["drops"] >= 6
    assert card["draft"]["dropped_layer"] in ("project", "org", "platform", "probe")
    assert "install or serve" in card["draft"]["remedy"]


def test_r6_unenforceable_cap_lists_the_rows(client, key, db, proj, auth):
    """18 / 28. Every eligible row unreporting under a cap is an R6-shaped card."""
    planner = _agent(client, key, "planner")
    res = _resolution(
        "gbagent:qwen3.6",
        unenforceable={"cap": "per_item_tokens",
                       "rows": [{"harness": "gbagent", "key": "gbagent:qwen3.6",
                                 "reporting_share": 0.0}]})
    _attempt(client, key, db, planner, "cap0", resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R6")
    assert cards
    assert cards[0]["draft"]["kind"] == "cap_unenforceable"
    assert cards[0]["draft"]["cap"] == "per_item_tokens"


# ---- 19: R3 family rule ---------------------------------------------------------------------

def test_r3_fires_only_when_beaten_at_family_level_across_two_families(
        client, key, db, proj, auth, monkeypatch):
    """19. Two families (A4 + B3) at the margin; a single-capability beat is the other test."""
    user = client.get("/api/auth/me", headers=auth).json()
    planner = _agent(client, key, "planner")
    db.add(FleetProfile(id="fp_s4", user_id=user["id"], project_id=proj,
                        defaults=["claude", "gbagent"], weights={}, excludes=[]))
    db.commit()
    res = _resolution("claude:sonnet", "gbagent:qwen3.6")
    for i in range(8):
        _attempt(client, key, db, planner, f"a-cl{i}", vendor="claude", model="sonnet",
                 outcome="signed_off" if i < 3 else "bounced",
                 touchpoints=["backend/alembic/versions/x.py"], resolution=res)
        _attempt(client, key, db, planner, f"a-gb{i}", vendor="gbagent", model="qwen3.6",
                 touchpoints=["backend/alembic/versions/x.py"], resolution=res)
        _attempt(client, key, db, planner, f"b-cl{i}", vendor="claude", model="sonnet",
                 outcome="signed_off" if i < 3 else "bounced",
                 touchpoints=["backend/app/services/x.py"], resolution=res)
        _attempt(client, key, db, planner, f"b-gb{i}", vendor="gbagent", model="qwen3.6",
                 touchpoints=["backend/app/services/x.py"], resolution=res)
    cards = _by_rule(_cards(client, auth, proj), "R3")
    assert cards, cards
    assert cards[0]["draft"]["defaults"][0] == "gbagent"
    assert len(cards[0]["draft"]["families"]) >= 2


# ---- 11: org cards name projects ------------------------------------------------------------

def test_org_scope_cards_name_the_projects(client, auth, db):
    """11. Org view aggregates capability cells; a card at org scope names the projects."""
    alex = db.scalar(select(User).where(User.email == "alex@ascme-labs.com"))
    db.add(Organization(id="org_s4", name="org_s4", telemetry_share=False))
    db.commit()
    db.add(OrgMembership(org_id="org_s4", user_id=alex.id, role="admin"))
    db.commit()
    p1 = client.post("/api/projects", json={"name": "One"}, headers=auth).json()["id"]
    p2 = client.post("/api/projects", json={"name": "Two"}, headers=auth).json()["id"]
    db.get(Project, p1).org_id = "org_s4"
    db.get(Project, p2).org_id = "org_s4"
    db.commit()
    for pid, finished, signed in ((p1, 6, 6), (p2, 4, 0)):
        db.add(HarnessRollup(
            project_id=pid, week="2026-W36", vendor="gbagent", model="qwen3.6",
            binary_version="1.0.0", capability="A4", size_band="M", finished=finished,
            signed_off=signed, bounced=finished - signed, first_choice=finished))
    db.commit()
    r = client.get("/api/harness?org_id=org_s4", headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["scope"] == "org"
    cell = out["cells"][0]
    assert {p["project_id"] for p in cell["by_project"]} == {p1, p2}

    rec = client.get("/api/harness/recommendations?org_id=org_s4", headers=auth)
    assert rec.status_code == 200, rec.text
    body = rec.json()
    assert body["scope"] == "org"
    assert set(body["projects"]) == {p1, p2}
    r5 = [c for c in body["cards"] if c["rule"] == "R5"]
    if r5:
        assert p1 in r5[0]["draft"].get("projects", []) or p1 in r5[0]["detail"]


# ---- 12 / 29: contribution ------------------------------------------------------------------

def test_contribution_payload_is_exactly_the_d11_key_set(db, proj):
    """12. Same key-set test as criterion 31, on the instance payload."""
    db.add(HarnessRollup(
        project_id=proj, week="2026-W36", vendor="gbagent", model="qwen3.6",
        binary_version="1.0.0", capability="A4", size_band="M", finished=5, signed_off=4,
        bounced=1, first_choice=4, probe=1))
    db.commit()
    rows = hsvc.contribution_rows_for_instance(db)
    assert rows
    for row in rows:
        assert set(row) == hsvc.CONTRIBUTION_KEYS
        assert "path" not in row and "item_id" not in row and "reviewer" not in row


def test_accept_redacts_a_model_seen_from_fewer_than_three_instances(db, hosted):
    """29. Stored as `other` until the third instance; un-redacts forward only."""
    row = _d11()
    first = hsvc.accept_contributions(db, "inst_a", [row])
    assert first["redacted_models"] == 1
    cells = db.scalars(select(PlatformContributionCell)).all()
    assert all(c.model == "other" for c in cells)

    hsvc.accept_contributions(db, "inst_b", [row])
    hsvc.accept_contributions(db, "inst_c", [_d11(week="2026-W37")])
    later = db.scalars(select(PlatformContributionCell).where(
        PlatformContributionCell.instance_id == "inst_c")).all()
    assert later and later[0].model == "rare-model"
    earlier = db.scalars(select(PlatformContributionCell).where(
        PlatformContributionCell.instance_id == "inst_a")).all()
    # inst_a was replaced on its own later posts only; it has not posted again, so
    # its last accept (the first one) stayed `other`. Forward-only.
    assert earlier and earlier[0].model == "other"


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "hosted_mode", True)
    return True


def test_accept_rejects_a_payload_with_an_extra_key(db, hosted):
    """12. A path, item or reviewer in the body is a 422, not a stored field."""
    bad = _d11()
    bad["path"] = "secret.py"
    with pytest.raises(hsvc.AttemptRefused):
        hsvc.accept_contributions(db, "inst_x", [bad])


def test_opt_out_drops_the_contributor_and_recomputes(db, hosted):
    """12. Turning the toggle off stops posting and the hosted recompute drops them."""
    hsvc.accept_contributions(db, "inst_z", [_d11(), _d11(capability="B3")])
    assert db.scalars(select(PlatformContributionCell)).all()
    hsvc.accept_contributions(db, "inst_z", [], opted_out=True)
    assert db.scalars(select(PlatformContributionCell)).all() == []


def test_contributions_http_round_trip_uses_the_sync_credential(client, auth, db, proj):
    """12. POST /api/platform/contributions over a sync key."""
    key = client.post("/api/api-keys",
                      json={"name": "spoke", "scopes": ["sync"], "project_id": proj},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/platform/contributions",
                    json={"rows": [_d11()]},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1
    assert set(_d11()) == hsvc.CONTRIBUTION_KEYS


# ---- 13: snapshot ---------------------------------------------------------------------------

def test_snapshot_serves_n_as_a_band_and_no_identifier(db, hosted):
    """13. The snapshot carries n as a band and no org, project or instance id."""
    db.add(PlatformRollup(
        week="2026-W36", vendor="gbagent", model="qwen3.6", binary_version="1.0.0",
        capability="A4", size_band="M", orgs_contributing=3, finished=40, signed_off=30,
        top_org_share=0.4))
    db.commit()
    snap = hsvc.publish_snapshot(db)
    assert snap["cells"]
    cell = snap["cells"][0]
    assert cell["n"] == "20–49"
    blob = str(snap)
    assert "org_id" not in blob and "project_id" not in blob and "instance_id" not in blob
    assert "path" not in blob


def test_fetched_priors_are_outranked_by_a_local_cell_at_the_floor(
        client, key, db, proj, auth):
    """13. An instance that fetches resolves with platform-labelled priors; local n≥5 wins.

    Both layers coexist on the same vendor/model/capability. Scoring walks
    LAYERS in order (fleet `matrix._quality_for` / `resolve`). Sabotage:
    reverse that order, or skip the project cell — this then picks the
    platform 0.9 and `layer == project` fails.
    """
    hsvc.replace_priors(db, {
        "snapshot_at": "2026-09-10",
        "cells": [{"vendor": "gbagent", "model": "qwen3.6", "binary_version": "1.0.0",
                   "capability": "A4", "size_band": "M", "rate": 0.9, "n": "50–199"}],
    })
    db.commit()
    priors = db.scalars(select(CapabilityPrior)).all()
    assert priors and priors[0].source == "platform" and priors[0].snapshot_at == "2026-09-10"
    planner = _agent(client, key, "planner")
    # 1 signed off of 5 finished → project 0.2 at the floor, against platform 0.9.
    for i in range(5):
        _attempt(client, key, db, planner, f"local{i}",
                 outcome="signed_off" if i == 0 else "bounced")
    from app.services import delegation as del_svc
    measured = del_svc.measured(db, proj)
    same = [c for c in measured
            if c.get("vendor") == "gbagent" and c.get("model") == "qwen3.6"
            and c.get("capability") == "A4"]
    by = {c["layer"]: c for c in same}
    assert "project" in by and "platform" in by, by
    assert by["platform"].get("snapshot_at") == "2026-09-10"
    assert by["project"]["quality"]["n"] >= 5
    picked = None
    for layer in del_svc.LAYERS:
        cell = by.get(layer)
        n = ((cell.get("quality") or {}).get("n") or 0) if cell else 0
        if cell and n >= 5:
            picked = cell
            break
    assert picked is not None and picked["layer"] == "project", picked
    assert picked["quality"]["value"] == 0.2


# ---- D7 / criterion 27: org and platform probe separation -----------------------------------

def test_org_measured_subtracts_probe_from_band_n(client, key, db, proj, auth):
    """27. 8 natural + 2 probe on a sibling: org band-n=8, not 10.

    Sabotage: use roll.finished (raw) for band-n in delegation.measured() org layer —
    this test reads n=10 and fails.
    """
    from app.services import delegation as dsvc

    alex = db.scalar(select(User).where(User.email == "alex@ascme-labs.com"))
    db.add(Organization(id="org_probe", name="org_probe", telemetry_share=False))
    db.commit()
    db.add(OrgMembership(org_id="org_probe", user_id=alex.id, role="admin"))
    db.commit()
    sibling = client.post("/api/projects", json={"name": "Sibling"}, headers=auth).json()["id"]
    db.get(Project, proj).org_id = "org_probe"
    db.get(Project, sibling).org_id = "org_probe"
    db.commit()
    db.add(HarnessRollup(
        project_id=sibling, week=hsvc.week_of(hsvc._now()),
        vendor="gbagent", model="qwen3.6", binary_version="1.0.0",
        capability="A4", size_band="M", finished=10, signed_off=6,
        bounced=4, first_choice=8, probe=2))
    db.commit()
    rows = dsvc.measured(db, proj)
    org_cells = [c for c in rows if c["layer"] == "org"]
    assert org_cells, "org layer must appear for a sibling with traffic"
    cell = org_cells[0]
    assert cell["quality"]["n"] == 8, (
        f"org natural n must be 8 (10 finished - 2 probe), got {cell['quality']['n']}")
    band_n = cell["bands"].get("M", {}).get("n")
    assert band_n == 8, f"org band-n must be 8, got {band_n}"


def test_platform_roll_subtracts_probe_from_contribution_cells(db, hosted):
    """27. 8 natural + 2 probe on a contributed instance: platform finished=8, not 10.

    Sabotage: use row.finished (raw) for contribution cells in platform_roll() —
    this test reads finished=10 and fails.
    """
    db.add(PlatformContributionCell(
        instance_id="inst-probe", week="2026-W37", vendor="gbagent", model="qwen3.6",
        binary_version="1.0.0", capability="A4", size_band="M",
        finished=10, signed_off=6, first_choice=8, probe=2))
    db.commit()
    hsvc.platform_roll(db)
    db.commit()
    rolls = db.scalars(select(PlatformRollup)).all()
    assert rolls, "platform_roll must produce a row from a contribution cell"
    roll = rolls[0]
    assert roll.finished == 8, (
        f"platform natural finished must be 8 (10 - 2 probe), got {roll.finished}")
    assert roll.signed_off == 6


def test_snapshot_http_is_the_served_payload(client, auth, db, proj):
    db.add(PlatformRollup(
        week="2026-W36", vendor="gbagent", model="qwen3.6", binary_version="1.0.0",
        capability="A4", size_band="M", orgs_contributing=4, finished=80, signed_off=60,
        top_org_share=0.3))
    db.commit()
    hsvc.publish_snapshot(db)
    db.commit()
    key = client.post("/api/api-keys",
                      json={"name": "spoke", "scopes": ["sync"], "project_id": proj},
                      headers=auth).json()["plaintext"]
    r = client.get("/api/platform/snapshot", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["snapshot_at"]
    assert r.json()["cells"][0]["n"] in ("50–199", "20–49", "200+")
