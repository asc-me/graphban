"""GRPH-932 — an agent that is calling us is not offline.

**Accept:** any tool call on the owning credential refreshes the caller's presence. Reads
count. Refusals count. The item lease and `state` are untouched, and a call on someone
else's credential refreshes nothing.

**What went wrong.** Presence was refreshed by `heartbeat` alone. A worker that spent its
turns READING — `get_item_details`, `search_code`, a long file dump — sent no heartbeat, so
after the 150s TTL the server declared it offline, the supervisor released its item
(GRPH-850), and its next heartbeat came back `not the lease holder`. The child then reported
honestly and exited without writing a line.

Measured on wave p48a (PRD-48 fleet hardening, qwen-code qwen3.8-max, 2026-09-29) — four of
six children, in one wave:

    p48a-1  GRPH-A3017  GRPH-983   15 turns    951,247 tokens
    p48a-2  GRPH-A3018  GRPH-982   30 turns  2,378,129 tokens
    p48a-3  GRPH-A3019  GRPH-984   25 turns  1,898,959 tokens
    p48a-5  GRPH-A3021  GRPH-985   21 turns  1,501,416 tokens
                                   TOTAL   6,729,751 tokens

6.73 million tokens for zero deliverables, and every child exited `success` with
`is_error: false` — so nothing in the wave's own report said anything was wrong. That is why
this is a server-side fix rather than a plea for better heartbeat discipline in the children:
reading is not a failure to work, and the server had the evidence in its own request log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent
from app.services import fleet as fleet_svc


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Presence"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "pres", "project_id": proj},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def other_key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "other", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _agent(client, key, label="w"):
    return _ok(client, key, "register_agent", {"branch": "gb/p-1", "label": label})["agent_id"]


def _row(client, agent_id) -> Agent:
    """The stored row, read through the app's own session."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        return db.get(Agent, agent_id)


def _age(client, agent_id, seconds: int) -> None:
    """Backdate `last_seen_at`, standing in for time passing."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        row = db.get(Agent, agent_id)
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.commit()


# ---- the regression ---------------------------------------------------------------------

def test_an_agent_that_only_reads_stays_online_past_the_ttl(client, key):
    """THE ONE THAT MATTERS. Four children died on exactly this path. The agent never
    heartbeats; it only reads, which is what a worker does before it can write anything."""
    agent = _agent(client, key)
    _age(client, agent, fleet_svc.presence_ttl_seconds() + 30)
    assert fleet_svc.presence_state(_row(client, agent)) == "offline", "precondition"

    _ok(client, key, "get_backlog", {"agent_id": agent})

    assert fleet_svc.presence_state(_row(client, agent)) != "offline"


def test_reading_does_not_extend_the_item_lease(client, key):
    """Presence and the lease are different claims and must stay so. "I am alive" must not
    become "I am still working on that item" — a read of an unrelated row would otherwise
    hold a claim open indefinitely."""
    agent = _agent(client, key)
    _ok(client, key, "create_item", {"title": "a slice", "status": "next", "effort": 1})
    claimed = _ok(client, key, "claim_next", {"agent_id": agent})["item"]["id"]

    before = _row(client, agent)
    from app.db import SessionLocal
    from app.models import Item
    with SessionLocal() as db:
        lease_before = db.get(Item, claimed).claimed_at
    _age(client, agent, fleet_svc.seen_floor_seconds() + 5)

    _ok(client, key, "get_backlog", {"agent_id": agent})

    with SessionLocal() as db:
        assert db.get(Item, claimed).claimed_at == lease_before, \
            "a read refreshed the item lease; only heartbeat(id=) may do that"
    assert _row(client, agent).last_seen_at != before.last_seen_at, "presence did move"


def test_a_refused_call_still_proves_the_caller_is_alive(client, key):
    """The failure this fixes ENDED in a refusal — `not the lease holder`. An agent whose
    calls are being rejected is still running, and if a refusal did not count, an agent
    stuck in a refusal loop would be declared dead for being stuck."""
    agent = _agent(client, key)
    _age(client, agent, fleet_svc.presence_ttl_seconds() + 30)

    res = _rpc(client, key, "heartbeat", {"id": "GRPH-DOESNOTEXIST", "agent_id": agent})
    assert res.get("isError"), "expected a refusal to drive this test"

    assert fleet_svc.presence_state(_row(client, agent)) != "offline"


# ---- what it must NOT do ----------------------------------------------------------------

def test_another_credentials_call_refreshes_nothing(client, key, other_key):
    """Two guards stand here and this exercises the OUTER one: `_agent_for_call` already
    refuses an `agent_id` that is not on the calling key, so `seen` is never even reached.

    Found by sabotage: deleting the ownership check inside `seen` leaves this test green,
    because this path never depends on it. The inner guard is pinned separately below —
    a test that cannot fail for the reason it names is worse than no test."""
    agent = _agent(client, key)
    _age(client, agent, fleet_svc.presence_ttl_seconds() + 30)
    stamp = _row(client, agent).last_seen_at

    _ok(client, other_key, "get_backlog", {"agent_id": agent})

    assert _row(client, agent).last_seen_at == stamp
    assert fleet_svc.presence_state(_row(client, agent)) == "offline"


def test_seen_refuses_an_agent_on_another_credential(client, key, other_key):
    """The INNER guard, called directly, because the dispatcher path above can never reach
    it. Defence in depth: `seen` is a service function, and the next caller may not resolve
    ownership first. Without this check that caller could keep another credential's dead
    agent looking alive."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ApiKey

    agent = _agent(client, key)
    _age(client, agent, fleet_svc.presence_ttl_seconds() + 30)
    with SessionLocal() as db:
        mine = db.get(Agent, agent).api_key_id
        stranger = db.scalars(select(ApiKey).where(ApiKey.id != mine)).first()
        assert stranger is not None and stranger.id != mine, "need a second credential"

        assert fleet_svc.seen(db, agent, api_key_id=stranger.id) is False
        assert fleet_svc.seen(db, agent, api_key_id=mine) is True


def test_a_read_does_not_report_the_agent_as_working(client, key):
    """`touch` moves an agent to `working` because a heartbeat naming an item says what it is
    doing. An arbitrary read says only that it is there, and claiming otherwise would show a
    reviewer reading a diff as building."""
    agent = _agent(client, key)
    assert _row(client, agent).state == "idle", "precondition"
    _age(client, agent, fleet_svc.seen_floor_seconds() + 5)

    _ok(client, key, "get_backlog", {"agent_id": agent})

    assert _row(client, agent).state == "idle"


def test_a_quarantined_agent_cannot_read_its_way_back(client, key):
    """The verdict stays attached to the process that earned it. `touch` already refuses to
    un-quarantine; a presence refresh that moved `state` would have reopened that door."""
    agent = _agent(client, key)
    from app.db import SessionLocal
    with SessionLocal() as db:
        db.get(Agent, agent).state = "quarantined"
        db.commit()
    _age(client, agent, fleet_svc.seen_floor_seconds() + 5)

    _ok(client, key, "get_backlog", {"agent_id": agent})

    assert _row(client, agent).state == "quarantined"


def test_no_row_is_created_for_an_unknown_agent(client, key):
    """Resurrecting an id the roster had already aged out would hand it a second identity."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        assert fleet_svc.seen(db, "GRPH-A999999", api_key_id=None) is False
        assert db.get(Agent, "GRPH-A999999") is None


def test_a_credential_identity_is_not_an_agent(client, key):
    """`caller_identity` returns a `credential:…` marker when no agent is named. That is not
    a row and must not be looked up as one."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        marker = fleet_svc.caller_identity(None, type("K", (), {"name": "k", "id": "k"})())
        assert fleet_svc.is_credential(marker)
        assert fleet_svc.seen(db, marker) is False


# ---- the write-rate floor ---------------------------------------------------------------

def test_a_fresh_stamp_is_not_rewritten_on_every_call(client, key):
    """One commit per read would make a chatty agent expensive. The floor is half a heartbeat
    interval, so a skipped write can never leave a stamp old enough to read as offline."""
    agent = _agent(client, key)
    stamp = _row(client, agent).last_seen_at

    for _ in range(3):
        _ok(client, key, "get_backlog", {"agent_id": agent})

    assert _row(client, agent).last_seen_at == stamp, "rewrote a stamp inside the floor"


def test_the_floor_is_shorter_than_the_ttl_by_a_wide_margin():
    """The invariant that makes the floor safe, pinned so a future tuning cannot break it
    silently: a stamp skipped at the floor is still far younger than offline."""
    assert fleet_svc.seen_floor_seconds() < fleet_svc.heartbeat_interval_seconds()
    assert fleet_svc.seen_floor_seconds() * 3 < fleet_svc.presence_ttl_seconds()
