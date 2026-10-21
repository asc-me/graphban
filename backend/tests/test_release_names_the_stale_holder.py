"""GRPH-1057 — a release refusal names the holder it found, and a dead bound seat requeues.

Field shape: a child exits, the item stays `in_progress` under that agent, and `release_item`
from anyone else comes back "not the lease holder" / "another agent holds it". That sentence
claims a holder whether `claimed_by` is empty, points at a row that is gone, or points at an
agent the roster already calls offline. The sweep that should have cleared the dead child is
`requeue_offline_items`, run from `fleet_status`. It does not read `enrolment_id`, so a bound
seat is the same death as an unbound one — this file is what fails if that stops being true.

The message keeps the words `not the lease holder`. gbfleet classifies a moved lease by that
phrase, and dropping it turns a non-event into a wave failure.

Sabotage, each one red on its own:
- `if agent.enrolment_id: return False` inside `sweep_offline` leaves the dead bound child
  holding after `fleet_status`. The unbound child still clears, so a test that only aged an
  unbound agent would stay green.
- deleting the `requeue_offline_items` call in `fleet_status` leaves every dead holder in
  place. A test that called `requeue_offline_items` itself would stay green.
- restoring the hint "the lease expired or another agent holds it" fails the refusal
  assertions below, which go through the MCP tool and not `release_refusal` directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Agent, ApiKey, AreaReservation
from app.services import fleet as fleet_svc
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "StaleHolder"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
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
    return _ok(_mcp(client, key, "register_agent",
                    {"branch": "gb/test", "label": label, **kw}))["agent_id"]


def _item(client, key, title="bind me", touchpoints=None) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next",
        "touchpoints": touchpoints or ["backend/app/services/x.py"]}))["id"]


def _bound(client, key, planner, title, touchpoints) -> tuple[str, str]:
    """Mint a bound seat, register the child, and return (item key, child agent id)."""
    item = _item(client, key, title, touchpoints)
    out = _ok(_mcp(client, key, "delegate", {
        "id": item, "lane": "backend", "tier": "cheap", "agent_id": planner, "seat": True}))
    reg = _ok(_mcp(client, key, "register_agent", {
        "branch": "gb/test", "label": title,
        "enrolment_code": out["enrolment_code"]}))
    assert reg["assigned"]["state"] == "claimed", reg["assigned"]
    return item, reg["agent_id"]


def _row(db, item_key):
    from app.models import Item
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


def _age(db, agent_id: str) -> None:
    db.expire_all()
    db.get(Agent, agent_id).last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds() + 30)
    db.commit()


def _reservations(db, agent_id: str) -> list:
    db.expire_all()
    return list(db.scalars(select(AreaReservation).where(AreaReservation.agent_id == agent_id)))


def _holdings(client, key, agent_id: str) -> list:
    roster = _ok(_mcp(client, key, "fleet_status", {}))
    row = next(a for a in roster["agents"] if a["id"] == agent_id)
    return row["holdings"]


# ---- the sweep, through fleet_status, and the refusal that points at it -------------------


def test_an_offline_bound_seat_is_requeued_and_named(client, key, db):
    """The death the ticket names: a bound child gone quiet, and an unbound one beside it.

    Presence is aged, the item lease is not. `claim_next` would still see the lease as live,
    so a green result here is the sweep and not the lease timeout. The refusal is taken
    BEFORE `fleet_status`, and the item must still be held afterwards — the hint points at
    the sweep, it does not perform it.
    """
    planner = _agent(client, key, "planner")
    other = _agent(client, key, "other")
    unbound_item = _item(client, key, "unbound")
    assert items_svc.claim_item(db, unbound_item, _agent(client, key, "unbound-holder")) is not None
    unbound_holder = _row(db, unbound_item).claimed_by
    dead_item, dead = _bound(client, key, planner, "dead-child", ["backend/app/services/dead.py"])
    live_item, live = _bound(client, key, planner, "live-child", ["backend/app/services/live.py"])
    assert _reservations(db, dead), "precondition: the bound seat reserved its areas"
    assert _reservations(db, live)

    _age(db, unbound_holder)
    _age(db, dead)

    err = _err(_mcp(client, key, "release_item", {"id": dead_item, "agent_id": other}))
    assert err["code"] == "conflict"
    assert "not the lease holder" in err["message"]
    assert dead in err["message"] and "offline" in err["message"]
    assert "requeue_offline_items" in err["hint"]
    assert "retire_wave" in err["hint"]
    assert "another agent holds" not in err["hint"]
    held = _row(db, dead_item)
    assert held.claimed_by == dead and held.status == "in_progress", (
        "the refusal released the item; only the sweep or the holder may")

    _ok(_mcp(client, key, "fleet_status", {}))

    cleared = _row(db, dead_item)
    assert cleared.claimed_by is None and cleared.status == "next"
    assert _reservations(db, dead) == []
    assert _holdings(client, key, dead) == []
    unbound = _row(db, unbound_item)
    assert unbound.claimed_by is None and unbound.status == "next", (
        "the sweep released the bound seat and left the unbound death")
    still = _row(db, live_item)
    assert still.claimed_by == live and still.status == "in_progress"
    assert _reservations(db, live)
    assert [h["id"] for h in _holdings(client, key, live)] == [live_item]


def test_a_live_bound_holder_is_named_and_not_handed_to_the_sweep(client, key, db):
    """A holder the roster calls idle is there. The hint names them and the force-release;
    it must not say the sweep will clear a live lease."""
    planner = _agent(client, key, "planner")
    other = _agent(client, key, "other")
    item, child = _bound(client, key, planner, "alive", ["backend/app/services/alive.py"])

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert "not the lease holder" in err["message"]
    assert child in err["message"] and "idle" in err["message"]
    assert "offline" not in err["message"]
    assert "retire_wave" in err["hint"]
    assert "requeue_offline_items" not in err["hint"]

    _ok(_mcp(client, key, "fleet_status", {}))
    still = _row(db, item)
    assert still.claimed_by == child and still.status == "in_progress"


def test_release_item_says_when_nobody_holds_the_lease(client, key, db):
    """`claimed_by` empty is not "another agent holds it". The old hint claimed a holder
    that was not there, which is the sentence a planner acted on."""
    other = _agent(client, key, "other")
    item = _item(client, key, "free")

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert "nobody holds it" in err["message"]
    assert "another agent" not in err["message"] and "another agent" not in err["hint"]
    assert _row(db, item).claimed_by is None


def test_a_holder_with_no_agent_row_is_named_and_left_alone(client, key, db):
    """No row is not offline. The sweep must not release an id it cannot find, and the
    refusal must not promise that it will."""
    other = _agent(client, key, "other")
    item = _item(client, key, "ghost")
    row = _row(db, item)
    row.claimed_by = "GRPH-A-NOSUCH"
    row.claimed_at = datetime.now(timezone.utc)
    row.status = "in_progress"
    db.commit()

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert "GRPH-A-NOSUCH" in err["message"]
    assert "no agent row" in err["message"]
    assert "will not clear" in err["hint"]
    assert "requeue_offline_items" not in err["hint"]

    _ok(_mcp(client, key, "fleet_status", {}))
    still = _row(db, item)
    assert still.claimed_by == "GRPH-A-NOSUCH" and still.status == "in_progress"


def test_a_quarantined_holder_is_not_released_as_offline(client, key, db):
    """Quarantine is a verdict, not a death. The sweep leaves the lease, and the refusal
    says so rather than pointing at `requeue_offline_items`."""
    other = _agent(client, key, "other")
    holder = _agent(client, key, "quarantined")
    item = _item(client, key, "held")
    assert items_svc.claim_item(db, item, holder) is not None
    db.expire_all()
    db.get(Agent, holder).state = "quarantined"
    db.commit()

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert holder in err["message"] and "quarantined" in err["message"]
    assert "does not release" in err["hint"]
    assert "requeue_offline_items" not in err["hint"]

    _ok(_mcp(client, key, "fleet_status", {}))
    still = _row(db, item)
    assert still.claimed_by == holder and still.status == "in_progress"


def test_a_revoked_key_is_offline_while_the_stamp_is_fresh(client, key, auth, proj, db):
    """The roster says offline the moment the credential is revoked, without backdating
    `last_seen_at`. The refusal has to use that word, or it describes a holder the sweep
    is about to release as still idle."""
    raw = client.post("/api/api-keys", json={"name": "doomed", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    holder = _agent(client, raw, "doomed")
    other = _agent(client, key, "other")
    item = _item(client, key, "on a revoked key")
    assert items_svc.claim_item(db, item, holder) is not None
    db.expire_all()
    seen = db.get(Agent, holder).last_seen_at
    db.get(ApiKey, db.get(Agent, holder).api_key_id).revoked = True
    db.commit()

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert holder in err["message"] and "offline" in err["message"]
    assert "idle" not in err["message"]
    assert "requeue_offline_items" in err["hint"]
    assert "retire_wave" not in err["hint"], "this holder is not on a seat retire_wave can see"

    _ok(_mcp(client, key, "fleet_status", {}))
    cleared = _row(db, item)
    assert cleared.claimed_by is None and cleared.status == "next"
    db.expire_all()
    assert db.get(Agent, holder).last_seen_at == seen, "revocation must not falsify the stamp"


def test_an_empty_build_lease_with_a_review_hold_names_the_reviewer(client, key, db):
    """'Nobody holds it' would be true of the build lease and a lie about the item. A
    reviewer is still holding it, and the refusal has to say so."""
    reviewer = _agent(client, key, "reviewer")
    other = _agent(client, key, "other")
    item = _item(client, key, "in review")
    row = _row(db, item)
    row.status = "review"
    row.review_claimed_by = reviewer
    row.review_claimed_at = datetime.now(timezone.utc)
    row.claimed_by = None
    db.commit()

    err = _err(_mcp(client, key, "release_item", {"id": item, "agent_id": other}))
    assert "no build lease" in err["message"]
    assert reviewer in err["message"]
    assert "nobody holds it" not in err["message"]
