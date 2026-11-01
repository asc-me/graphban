"""GRPH-1254 — a review row says when its hold has lapsed, so a supervisor need not guess.

Observed on the GRPH-P62 wave: GRPH-1248 sat in `review` for over an hour with a green PR. Its
`review_claimed_by` named GRPH-A7019, which was offline and retired, and `claim_review` would
already have handed the item to anybody who asked. But `gbfleet until` decides whether to spawn a
reviewer from `search_items(status="review", fields="full")`, and that row carried only the raw
column. A dead holder read as a live one, so no reviewer was ever spawned and the wave idled with
`held by review; no expiry reported` on every cycle.

The row now carries `review_claim_lapse`, from `fleet.review_claim_lapse` (the one definition of
a live hold), and only when the hold HAS lapsed. These tests read the row exactly the way the
supervisor does.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent
from app.services import fleet as fleet_svc


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
    return client.post("/api/projects", json={"name": "Lapsed holds"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _agent(client, key, label) -> str:
    # One credential, so each agent declares its own `instance` (see test_a_reviewer_is_not_idle).
    return _ok(_mcp(client, key, "register_agent",
                    {"branch": "gb/test", "label": label, "role_hint": "worker",
                     "capabilities": {"instance": label}}))["agent_id"]


def _item_in_review(client, key, proj, builder) -> str:
    made = _ok(_mcp(client, key, "create_item",
                    {"project_id": proj, "title": "something to review", "effort": 3}))
    for status in ("in_progress", "review"):
        _ok(_mcp(client, key, "update_item",
                 {"project_id": proj, "id": made["id"], "status": status, "agent_id": builder}))
    return made["id"]


def _review_row(client, key, proj, item_id) -> dict:
    """The supervisor's read, verbatim: `gbfleet.until._review_rows`."""
    rows = _ok(_mcp(client, key, "search_items",
                    {"project_id": proj, "status": "review", "fields": "full",
                     "limit": 10_000}))["results"]
    [row] = [r for r in rows if r["id"] == item_id]
    return row


def _stop_heartbeating(db, agent_id: str) -> None:
    db.get(Agent, agent_id).last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds() + 60)
    db.commit()


def test_a_dead_holders_row_says_the_hold_has_lapsed(client, key, proj, db):
    """THE DEFECT. The hold is young, so only presence can lapse it, which is exactly the
    P62 case. Sabotage: drop the `review_claim_lapse` block from `items.item_dict` and the row
    goes back to naming a dead reviewer with nothing to say it is dead."""
    builder = _agent(client, key, "builder")
    dead = _agent(client, key, "the reviewer that died")
    item_id = _item_in_review(client, key, proj, builder)
    assert _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": dead})).get("claimed")

    _stop_heartbeating(db, dead)

    row = _review_row(client, key, proj, item_id)
    assert row["review_claimed_by"] == dead, "the raw column must still say who held it"
    assert row["review_claim_lapse"] == "offline"


def test_a_live_holders_row_carries_no_lapse(client, key, proj, db):
    """The other direction: a reviewer doing the work keeps a row that reads as held, or the
    supervisor spawns a second reviewer onto it. Sabotage: always emit the field and this
    fails."""
    builder = _agent(client, key, "builder")
    reviewer = _agent(client, key, "reviewer")
    item_id = _item_in_review(client, key, proj, builder)
    assert _ok(_mcp(client, key, "claim_review",
                    {"project_id": proj, "agent_id": reviewer})).get("claimed")
    _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))

    row = _review_row(client, key, proj, item_id)
    assert row["review_claimed_by"] == reviewer
    assert "review_claim_lapse" not in row


def test_an_unheld_row_carries_no_lapse(client, key, proj, db):
    """Nobody ever took it: nothing has lapsed, and the field is absent rather than empty, so
    the thousand ordinary rows in a listing cost nothing."""
    builder = _agent(client, key, "builder")
    item_id = _item_in_review(client, key, proj, builder)

    row = _review_row(client, key, proj, item_id)
    assert not row.get("review_claimed_by")
    assert "review_claim_lapse" not in row
