"""PRD-37 D7 / criterion 11 — the measured axes carry their sample size and are never pooled.

The server states counts per vendor × model × lane × tier; the supervisor decides whether a
count is enough. So the tests here pin the cells and the arithmetic, and the one sabotage
that matters: pooling lanes together makes the per-lane test fail.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Delegation
from app.services import delegation as dsvc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Measured"}, headers=auth).json()["id"]


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


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _finished(db, api, proj, *, vendor, model, lane, tier, outcome, minutes, n=1):
    """Finished delegations, written straight into the ledger: the arithmetic is the subject
    here, not the claim path (test_delegation.py owns that). Agents and items come through
    the API so they carry the numbers the models require."""
    client, key, auth = api
    delegator = _agent(client, key, f"planner-{vendor}-{lane}-{tier}")
    child = _agent(client, key, f"child-{vendor}-{model}-{lane}-{tier}",
                   capabilities={"vendor": vendor, "model": model, "tier": "local"})
    for i in range(n):
        item = client.post("/api/items", json={"title": f"{vendor} {lane} {tier} {outcome} {i}", "project_id": proj,
                                               "touchpoints": []}, headers=auth).json()
        db.add(Delegation(id=f"d-{item['id']}", project_id=proj, item_id=item["id"], delegated_by=delegator,
                          agent_id=child, linked_by="seat", lane=lane, requested_tier=tier,
                          declared_model=model or None, declared_tier="local", outcome=outcome,
                          created_at=T0, claimed_at=T0 + timedelta(minutes=1),
                          finished_at=T0 + timedelta(minutes=1 + minutes), lease_seconds=600))
    db.commit()


_AGENTS: dict[str, str] = {}


def _agent(client, key, label, **kw) -> str:
    """One agent per label per test (the dict is reset by the `api` fixture)."""
    if label not in _AGENTS:
        r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "register_agent", "arguments": {"label": label, **kw}}},
                        headers={"X-API-Key": key}).json()["result"]
        assert not r.get("isError"), r
        _AGENTS[label] = r["structuredContent"]["agent_id"]
    return _AGENTS[label]


@pytest.fixture()
def api(client, key, auth):
    _AGENTS.clear()
    return client, key, auth


def _cell(rows, vendor, model, lane, tier):
    return next(r for r in rows if (r["vendor"], r["model"], r["lane"], r["tier"]) == (vendor, model, lane, tier))


def test_quality_is_signed_off_over_finished_per_cell_with_n(db, api, proj):
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=10, n=3)
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="bounced", minutes=10, n=1)
    rows = dsvc.measured(db, proj)
    cell = _cell(rows, "gbagent", "q", "backend", "cheap")
    assert cell["quality"] == {"value": 0.75, "n": 4}


def test_lanes_and_tiers_are_never_pooled(db, api, proj):
    """PRD-35 named the bias: frontier only sees what cheap failed. One cell per lane and
    per tier requested — a backend success says nothing about the frontend cell."""
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=5, n=5)
    _finished(db, api, proj, vendor="gbagent", model="q", lane="frontend", tier="cheap", outcome="bounced", minutes=5, n=2)
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="frontier", outcome="bounced", minutes=5, n=1)
    rows = dsvc.measured(db, proj)
    assert _cell(rows, "gbagent", "q", "backend", "cheap")["quality"] == {"value": 1.0, "n": 5}
    assert _cell(rows, "gbagent", "q", "frontend", "cheap")["quality"] == {"value": 0.0, "n": 2}
    assert _cell(rows, "gbagent", "q", "backend", "frontier")["quality"] == {"value": 0.0, "n": 1}
    assert len(rows) == 3, "a pooled row appeared"
    assert not any(r["lane"] in ("any", "all") for r in rows)


def test_latency_is_the_median_claim_to_finish_folded_onto_the_hour(db, api, proj):
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=6, n=1)
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="bounced", minutes=30, n=1)
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="blocked", minutes=90, n=1)
    cell = _cell(dsvc.measured(db, proj), "gbagent", "q", "backend", "cheap")
    assert cell["latency"] == {"value": 0.5, "n": 3, "median_seconds": 1800.0}


def test_a_child_that_declared_nothing_is_counted_under_undeclared_not_dropped(db, api, proj):
    _finished(db, api, proj, vendor="", model="", lane="backend", tier="cheap", outcome="signed_off", minutes=5)
    rows = dsvc.measured(db, proj)
    assert rows and rows[0]["vendor"] == dsvc.UNDECLARED and rows[0]["model"] == dsvc.UNDECLARED


def test_open_and_closed_delegations_do_not_count_only_finished_ones(db, api, proj):
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=5)
    row = db.scalars(select(Delegation)).first()
    db.add(Delegation(id="d-open", project_id=proj, item_id=row.item_id, delegated_by=row.delegated_by,
                      lane="backend", requested_tier="cheap", lease_seconds=600))
    db.add(Delegation(id="d-closed", project_id=proj, item_id=row.item_id, delegated_by=row.delegated_by,
                      lane="backend", requested_tier="cheap", closed_reason="withdrawn", lease_seconds=600))
    db.commit()
    rows = dsvc.measured(db, proj)
    assert _cell(rows, "gbagent", "q", "backend", "cheap")["quality"]["n"] == 1
    # The unfinished rows have no agent, so counting them would surface as an `undeclared`
    # cell rather than inflate this one — the sabotage this first version missed.
    assert len(rows) == 1, f"an unfinished delegation was counted: {rows}"


def test_fleet_status_carries_measured_and_an_empty_ledger_is_an_empty_list_not_an_absence(client, key, proj, db, api):
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert status["measured"] == []
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=5, n=2)
    status = _mcp(client, key, "fleet_status", {"project_id": proj})
    assert _cell(status["measured"], "gbagent", "q", "backend", "cheap")["quality"] == {"value": 1.0, "n": 2}


def test_measured_is_scoped_to_the_project(db, api, proj, client, auth):
    other = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    _finished(db, api, proj, vendor="gbagent", model="q", lane="backend", tier="cheap", outcome="signed_off", minutes=5)
    _finished(db, api, other, vendor="claude", model="opus", lane="backend", tier="frontier", outcome="signed_off", minutes=5)
    rows = dsvc.measured(db, proj)
    assert [r["vendor"] for r in rows] == ["gbagent"]


def test_a_declared_vendor_with_no_model_is_that_vendors_default_cell_not_undeclared(db, api, proj):
    """GRPH-732: qwen-code's matrix row names no model because the binary does not enforce one;
    a child that declared vendor=alibaba and no model must land in the ("alibaba", "") cell that
    row matches, not in undeclared."""
    _finished(db, api, proj, vendor="alibaba", model="", lane="backend", tier="cheap", outcome="signed_off", minutes=5)
    rows = dsvc.measured(db, proj)
    assert (rows[0]["vendor"], rows[0]["model"]) == ("alibaba", "")
