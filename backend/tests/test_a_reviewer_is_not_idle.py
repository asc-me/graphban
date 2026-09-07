"""GRPH-771 — a reviewer holding a claim was reported `idle`, and `reviewing` was unreachable.

Observed on the deployed instance: GRPH-A142 held a review claim on GRPH-767 for fifteen
minutes while the roster said `idle` and `last_seen` was 26 seconds old. That read as a
contradiction — a live agent holding work while claiming to have none.

It was not. A review claim is not an item lease: the lease is `claimed_by`, the hold is
`review_claimed_by`. So a reviewer heartbeats WITHOUT an item id, and that branch of
`heartbeat` hardcoded `idle`. `"reviewing"` was in `STATES` and no code path ever set it, so
the roster had one word for "between tasks" and "reviewing right now".

The ticket's own first direction — expire a review hold on AGE — turned out to exist already:
`review_claim_holder` lapses after `DEFAULT_LEASE_SECONDS` whether the holder is alive or not.
The message telling reviewers otherwise is fixed here too.
"""
from __future__ import annotations

import pytest

from app.services import fleet as fleet_svc
from app.services.items import DEFAULT_LEASE_SECONDS


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
    return client.post("/api/projects", json={"name": "Reviewing"},
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


def _agent(client, key, label, role) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "role_hint": role}))["agent_id"]


def _item_in_review(client, key, proj, builder) -> str:
    made = _ok(_mcp(client, key, "create_item",
                    {"project_id": proj, "title": "something to review", "effort": 3}))
    _ok(_mcp(client, key, "update_item",
             {"project_id": proj, "id": made["id"], "status": "in_progress",
              "agent_id": builder}))
    _ok(_mcp(client, key, "update_item",
             {"project_id": proj, "id": made["id"], "status": "review",
              "agent_id": builder}))
    return made["id"]


def test_a_reviewer_holding_a_claim_is_reviewing_not_idle(client, key, proj, db):
    """THE DEFECT. Sabotage: hardcode `idle` again and the roster goes back to describing a
    working reviewer and an agent between tasks with the same word."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)

    claimed = _ok(_mcp(client, key, "claim_review",
                       {"project_id": proj, "agent_id": reviewer}))
    assert claimed.get("claimed"), claimed

    beat = _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))
    assert beat["state"] == "reviewing"


def test_an_agent_holding_nothing_is_still_idle(client, key, proj, db):
    """The other half. "Between tasks" is a real state and must not be dressed up as work."""
    lone = _agent(client, key, "nobody's reviewer", "worker")
    assert _ok(_mcp(client, key, "heartbeat", {"agent_id": lone}))["state"] == "idle"


def test_an_expired_hold_does_not_keep_an_agent_looking_busy(client, key, proj, db):
    """A claim lapses on a clock. An agent still filed as `reviewing` on the strength of a
    dead hold would be the same defect pointing the other way."""
    from datetime import datetime, timedelta, timezone

    from app.models import Item

    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    item_id = _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))

    row = db.get(Item, item_id)
    row.review_claimed_at = (datetime.now(timezone.utc)
                             - timedelta(seconds=DEFAULT_LEASE_SECONDS + 60))
    db.commit()

    assert _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))["state"] == "idle"


def test_the_queue_says_how_long_a_hold_has_run_and_who_holds_it(client, auth, key, proj, db):
    """A hold renders identically to progress on the board, which is why the deployed
    diagnosis needed a database query. Sabotage: drop the two fields and it needs one again."""
    builder = _agent(client, key, "builder", "worker")
    reviewer = _agent(client, key, "reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))
    _ok(_mcp(client, key, "heartbeat", {"agent_id": reviewer}))

    queue = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"]
    row = queue[0]
    assert row["reviewed_by"] == reviewer
    assert row["held_for_seconds"] is not None and row["held_for_seconds"] < 60
    assert row["holder_state"] == "reviewing"


def test_an_unheld_item_reports_no_hold_rather_than_zero(client, auth, key, proj, db):
    """Zero seconds held is a duration; nothing holding it is not. Reading the first as the
    second is how "held for 0s" ends up rendered next to an item nobody has looked at."""
    builder = _agent(client, key, "builder", "worker")
    _item_in_review(client, key, proj, builder)

    row = client.get(f"/api/fleet?project_id={proj}",
                     headers=auth).json()["review_queue"][0]
    assert row["reviewed_by"] is None
    assert row["held_for_seconds"] is None
    assert row["holder_state"] is None


def test_the_reason_names_the_clock_not_silence(client, key, proj, db):
    """The ticket's first direction — expire a hold on AGE — already existed; the message
    describing it did not. A reviewer told to wait for the holder to go silent waits for a
    signal that never comes."""
    builder = _agent(client, key, "builder", "worker")
    first = _agent(client, key, "first reviewer", "worker")
    second = _agent(client, key, "second reviewer", "worker")
    _item_in_review(client, key, proj, builder)
    _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": first}))

    reason = fleet_svc.review_block_reason(db, agent_id=second, project_id=proj)
    assert "silent" not in reason, "it lapses on a clock, held or not"
    assert f"{DEFAULT_LEASE_SECONDS // 60} minutes" in reason
