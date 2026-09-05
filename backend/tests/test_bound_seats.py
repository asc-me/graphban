"""PRD-36 PR 1 — bound seats: a delegation mints the seat its child runs on, the seat carries
the item, registering on it claims the item, and the delegator may not review the result.

Each test names the acceptance criterion it pins (§7) and, where it matters, the sabotage
run against it. Rows are read through a session opened after the request so the app's own
commit is what is being inspected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Agent, AreaReservation, Delegation, Enrolment, Item
from app.services import delegation as dsvc
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
    return client.post("/api/projects", json={"name": "Bound"}, headers=auth).json()["id"]


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
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _item(client, key, title="bind me", touchpoints=None, **kw) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next",
        "touchpoints": touchpoints or ["backend/app/services/x.py"], **kw}))["id"]


def _delegate(client, key, item_id, who, seat=True, tier="cheap", **kw):
    return _mcp(client, key, "delegate", {"id": item_id, "lane": "backend", "tier": tier,
                                          "agent_id": who, "seat": seat, **kw})


def _stored(db, item_key) -> Item:
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


def _seat_row(db, code_prefix_from_code: str) -> Enrolment:
    db.expire_all()
    rows = db.scalars(select(Enrolment).order_by(Enrolment.created_at.desc())).all()
    assert rows, "no seat was minted"
    return rows[0]


# ---- 1–3: minting the bound seat -------------------------------------------------------------

def test_delegate_with_seat_mints_a_seat_bound_to_the_item(client, key, db):
    """1 / D1, D2. Sabotage: drop `item_id` from the mint call — the row has no item."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    assert out["enrolment_code"].startswith("WORKER-")
    seat = _seat_row(db, out["enrolment_code"])
    assert seat.item_id == _stored(db, item).id
    assert seat.delegation_id == out["delegation_id"]
    assert seat.minted_by == planner and seat.role == "worker"


def test_delegate_without_seat_mints_nothing(client, key, db):
    """1, second half."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner, seat=False))
    assert out["enrolment_code"] is None
    db.expire_all()
    assert db.scalars(select(Enrolment)).all() == []


def test_a_worker_cannot_mint_a_bound_seat_and_writes_no_delegation(client, auth, key, proj, db):
    """2 / D2. The mint gate is the mint gate: a worker seat may not bind one. Sabotage: mint
    after the delegation row is written — a refused mint then leaves a row behind."""
    code = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    me = _agent(client, key, "worker", enrolment_code=code)
    item = _item(client, key)
    e = _err(_delegate(client, key, item, me))
    assert e["code"] == "unauthorized", e
    db.expire_all()
    assert db.scalars(select(Delegation)).all() == []
    assert [r for r in db.scalars(select(Enrolment)).all() if r.item_id] == []


def test_a_bound_reviewer_seat_cannot_be_minted(db, proj):
    """3 / D1."""
    item = Item(id="it-r", project_id=proj, number=1, title="x", status="next")
    db.add(item); db.commit()
    with pytest.raises(ValueError, match="worker-only"):
        fleet_svc.issue_enrolment(db, project_id=proj, role="reviewer", item_id="it-r")


def test_a_bound_seat_is_refused_when_the_areas_are_held(client, key, proj, db):
    """D13 / risk 1. The divvy's collision check, made at mint time, naming the holder."""
    planner = _agent(client, key, "planner")
    holder = _agent(client, key, "holder")
    first = _item(client, key, "held", touchpoints=["backend/app/services/x.py"])
    got = fleet_svc.claim_cluster(db, agent_id=holder, project_id=proj)
    assert got["claimed"], got
    second = _item(client, key, "colliding", touchpoints=["backend/app/services/x.py"])
    e = _err(_delegate(client, key, second, planner))
    assert e["code"] == "conflict" and holder in e["message"]
    db.expire_all()
    assert not db.scalars(select(Delegation)).all(), "a refused mint writes nothing"
    # Without a seat the same delegation is accepted: the divvy will decide.
    assert _ok(_delegate(client, key, second, planner, seat=False))["state"] == "open"


# ---- 4–9: claim at registration --------------------------------------------------------------

def test_registering_on_a_bound_seat_claims_links_and_reserves_in_one_request(client, key, proj, db):
    """4 / D3, D5. Sabotage: skip the reservation loop and the collision test below fails."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, touchpoints=["backend/app/services/x.py", "web/src/a.tsx"])
    out = _ok(_delegate(client, key, item, planner))
    reg = _ok(_mcp(client, key, "register_agent", {
        "label": "child", "enrolment_code": out["enrolment_code"],
        "capabilities": {"model": "qwen3.6:35b", "tier": "cheap", "vendor": "gbagent"}}))
    child = reg["agent_id"]
    assert reg["assigned"] == {"item": item, "state": "claimed", "reason": None, "held_by": None}
    stored = _stored(db, item)
    assert stored.status == "in_progress" and stored.claimed_by == child
    areas = {r.area for r in db.scalars(select(AreaReservation).where(AreaReservation.agent_id == child)).all()}
    assert areas == {"backend/app/services/x.py", "web/src/a.tsx"}
    d = db.get(Delegation, out["delegation_id"]); db.refresh(d)
    assert d.agent_id == child and d.linked_by == "seat" and dsvc.state(d) == "claimed"
    assert d.declared_model == "qwen3.6:35b"
    # A second child cannot claim a cluster that collides with the reserved areas.
    other = _agent(client, key, "other")
    colliding = _item(client, key, "collides", touchpoints=["backend/app/services/x.py"])
    got = fleet_svc.claim_cluster(db, agent_id=other, project_id=proj)
    assert not got["claimed"] and child in got["reason"], got


def test_a_taken_item_registers_the_child_and_says_who_holds_it(client, key, db):
    """5 / D4, D14, D15."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    stranger = _agent(client, key, "stranger")
    assert items_svc.claim_item(db, item, stranger) is not None
    reg = _ok(_mcp(client, key, "register_agent", {"label": "late child",
                                                    "enrolment_code": out["enrolment_code"]}))
    assert reg["assigned"]["state"] == "taken"
    assert reg["assigned"]["reason"] == "held" and reg["assigned"]["held_by"] == stranger
    assert db.get(Agent, reg["agent_id"]) is not None, "registration is never refused for the claim"
    d = db.get(Delegation, out["delegation_id"]); db.refresh(d)
    assert d.closed_reason == "superseded" and d.closed_by == stranger


def test_a_pinned_item_reads_taken_with_the_pin_holder(client, key, db):
    """6. The pin arises AFTER the mint: somebody claims, gets bounced, and holds the pin when
    the child arrives."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    author = _agent(client, key, "author")
    assert items_svc.claim_item(db, item, author) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": author}))
    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "rev"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "no tests"}))
    reg = _ok(_mcp(client, key, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    assert reg["assigned"]["state"] == "taken"
    assert reg["assigned"]["reason"] == "pinned" and reg["assigned"]["held_by"] == author


def test_delegate_during_a_pin_is_refused_unless_the_author_is_your_child(client, key, db):
    """PRD-35 D15 still holds with seat=true."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    author = _agent(client, key, "author")
    assert items_svc.claim_item(db, item, author) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": author}))
    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "rev"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "no tests"}))
    e = _err(_delegate(client, key, item, planner))
    assert e["code"] == "conflict" and "pinned" in e["message"]


def test_an_unbound_seat_reads_assigned_none(client, key, proj, db):
    """7 / D4: a word, never a missing key."""
    _, code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker")
    reg = _ok(_mcp(client, key, "register_agent", {"label": "free", "enrolment_code": code}))
    assert reg["assigned"] == {"item": None, "state": "none", "reason": None, "held_by": None}
    reg2 = _ok(_mcp(client, key, "register_agent", {"label": "no seat at all"}))
    assert reg2["assigned"]["state"] == "none"


def test_a_blocked_item_reads_taken_blocked(client, key, db):
    """D15: the reason is the one every claim path gives."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    _ok(_mcp(client, key, "update_item", {"id": item, "blocker": "waiting on a decision"}))
    reg = _ok(_mcp(client, key, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    assert reg["assigned"]["state"] == "taken" and reg["assigned"]["reason"] == "blocked"


def test_reservation_failure_hands_the_claim_back(client, key, db, monkeypatch):
    """D14, the unit. Sabotage: remove the release in the except branch and the item stays
    claimed by a child whose areas were never reserved."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    from app.services import collision as collision_svc

    def boom(*a, **k):
        raise RuntimeError("no areas today")
    monkeypatch.setattr(collision_svc, "touch_areas", boom)
    reg = _ok(_mcp(client, key, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    assert reg["assigned"]["state"] == "taken"
    assert reg["assigned"]["reason"].startswith("reservation failed")
    stored = _stored(db, item)
    assert stored.claimed_by is None and stored.status == "next"


def test_a_dead_child_leaves_the_item_to_lapse_on_its_lease(client, key, db):
    """9. Nothing new is held past the lease: another agent can take it once the lease is stale."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    reg = _ok(_mcp(client, key, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    stored = _stored(db, item)
    stored.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=items_svc.DEFAULT_LEASE_SECONDS + 5)
    for r in db.scalars(select(AreaReservation).where(AreaReservation.agent_id == reg["agent_id"])).all():
        r.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()
    other = _agent(client, key, "other")
    assert items_svc.claim_item(db, item, other) is not None


# ---- 16: the delegator may not review its child's work -------------------------------------

def _delegated_and_in_review(client, key, db, planner, title="work"):
    item = _item(client, key, title)
    out = _ok(_delegate(client, key, item, planner))
    reg = _ok(_mcp(client, key, "register_agent", {"label": f"child-{title}", "enrolment_code": out["enrolment_code"],
                                                    "capabilities": {"instance": f"c-{title}"}}))
    child = reg["agent_id"]
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    return item, child


def test_the_delegator_cannot_sign_off_its_delegation_but_may_bounce(client, key, db):
    """16 / D19. Sabotage: look up the delegation by delegated_by instead of by the builder —
    the refusal then fires on an item the planner delegated to somebody who never built it."""
    planner = _agent(client, key, "planner", capabilities={"instance": "p"})
    item, child = _delegated_and_in_review(client, key, db, planner)
    e = _err(_mcp(client, key, "sign_off", {"id": item, "agent_id": planner,
                                             "evidence": [{"kind": "note", "detail": "looks fine"}]}))
    assert "delegated" in e["message"]
    # claim_review skips it for the delegator...
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": planner}))
    assert got["claimed"] is False
    # ...and an independent reviewer gets it and may sign it off.
    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "r"})
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] is True and got["item"]["id"] == item
    # The delegator may still bounce: rejecting is not approving.
    _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": reviewer}))
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": planner, "reason": "not what I asked"}))


def test_a_delegation_to_somebody_else_does_not_block_review_of_a_different_builder(client, key, db):
    """16 sabotage guard: the refusal keys on the delegation LINKED TO THE BUILDER."""
    planner = _agent(client, key, "planner", capabilities={"instance": "p"})
    item = _item(client, key)
    _ok(_delegate(client, key, item, planner, seat=False))  # open delegation, nobody claims it
    stranger = _agent(client, key, "stranger", capabilities={"instance": "s"})
    assert items_svc.claim_item(db, item, stranger) is not None  # supersedes
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": stranger}))
    assert not fleet_svc.delegated_by(db, _stored(db, item), planner)


# ---- PR 2 / D15: the roster carries what a bound seat handed each agent ----------------------

def test_the_roster_reads_assigned_from_the_seat_and_the_item(client, key, proj, db):
    """`spawn` echoes this, so the parent learns at registration. Derived at read time —
    `claimed` while the child holds the seat's item, `taken` with the holder once it does
    not, None on an unbound seat."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    out = _ok(_delegate(client, key, item, planner))
    reg = _ok(_mcp(client, key, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    child = reg["agent_id"]
    rows = {a["id"]: a for a in _ok(_mcp(client, key, "fleet_status", {}))["agents"]}
    assert rows[child]["assigned"] == {"item": item, "state": "claimed", "held_by": None}
    assert rows[planner]["assigned"] is None
    # The lease lapses and a stranger takes the item: the same row now reads taken.
    stored = _stored(db, item)
    stored.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=items_svc.DEFAULT_LEASE_SECONDS + 5)
    for r in db.scalars(select(AreaReservation).where(AreaReservation.agent_id == child)).all():
        r.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()
    stranger = _agent(client, key, "stranger")
    assert items_svc.claim_item(db, item, stranger) is not None
    rows = {a["id"]: a for a in _ok(_mcp(client, key, "fleet_status", {}))["agents"]}
    assert rows[child]["assigned"] == {"item": item, "state": "taken", "held_by": stranger}


# ---- the seat names its project (found by the criterion-18 check) ------------------------------

def test_registering_on_a_seat_without_naming_a_project_lands_on_the_seats_project(client, auth, proj, db):
    """A spawned child knows its code and its credential, not a project id. On a key that spans
    projects, the default project is not the seat's — the check's child was refused with
    "that seat belongs to a different project". The seat decides, within the key's scope."""
    other = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    wide = client.post("/api/api-keys", json={"name": "wide", "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]
    planner = _agent(client, wide, "planner", project_id=proj)
    item = _item(client, wide, project_id=proj)
    out = _ok(_delegate(client, wide, item, planner, project_id=proj))
    # No project_id on the registration — exactly what gbagent sends.
    reg = _ok(_mcp(client, wide, "register_agent", {"label": "child", "enrolment_code": out["enrolment_code"]}))
    child = db.get(Agent, reg["agent_id"])
    assert child.project_id == proj
    assert reg["assigned"]["state"] == "claimed" and reg["assigned"]["item"] == item
    # A project named explicitly still wins, and a seat from elsewhere is still refused there.
    e = _mcp(client, wide, "register_agent", {"label": "x", "enrolment_code": out["enrolment_code"], "project_id": other})
    assert e.get("isError")


def test_the_registration_reply_names_the_project_the_agent_landed_on(client, auth, proj, db):
    """GRPH-719: the child's later calls need a project to name; the reply is where it learns it."""
    reg = _ok(_mcp(client, key_for(client, auth, proj), "register_agent", {"label": "who-am-i"}))
    assert reg["project_id"] == proj


def key_for(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "k", "project_id": proj,
                                              "scopes": ["read", "write"]}, headers=auth).json()["plaintext"]
