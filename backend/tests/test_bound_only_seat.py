"""A bound seat may be minted bound-only, and then it stops after its own item (GRPH-1089).

A worker on `delegate(seat=true)` claims and builds that item, then carries on into
`claim_review` and reviews other agents' items. A `gbfleet until` wave relies on that, so
it stays the default. `bound_only` is the opt-in that stops it: once the bound item has
left `in_progress`, `claim_review`, `claim_next`, `claim_item`, `claim_cluster` and
`next_cluster` refuse and name the binding.

The refusal is an exception, not an empty result. None means "nothing was ready", and a
child that finished its one item would go home believing the board was clear.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.mcp_server import TOOLS, _FLEET_ONLY_ARGS
from app.models import Delegation, Enrolment, Item
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
    return client.post("/api/projects", json={"name": "BoundOnly"}, headers=auth).json()["id"]


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


def _item(client, key, title, touchpoints=None) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next",
        "touchpoints": touchpoints or ["backend/app/services/x.py"]}))["id"]


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "branch": "gb/test", **kw}))["agent_id"]


def _register(client, key, label, **kw) -> dict:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "branch": "gb/test", **kw}))


def _delegate(client, key, item_id, who, **kw):
    return _mcp(client, key, "delegate", {
        "id": item_id, "lane": "backend", "tier": "cheap", "agent_id": who,
        "seat": True, **kw})


def _stored(db, item_id) -> Item:
    db.expire_all()
    return db.get(Item, item_id)


def _seat(db) -> Enrolment:
    db.expire_all()
    rows = db.scalars(select(Enrolment).order_by(Enrolment.created_at.desc())).all()
    assert rows, "no seat was minted"
    return rows[0]


def _planner(client, auth, key, proj) -> str:
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _agent(client, key, "planner", enrolment_code=code)


def _in_review_by_somebody_else(client, key, db) -> str:
    """An item another agent built and moved to review, so claim_review can offer it."""
    who = _agent(client, key, "builder", worktree="/w/one", branch="gb/wave-1",
                 capabilities={"instance": "builder"})
    item = _item(client, key, "the work under review", ["backend/app/other.py"])
    assert items_svc.claim_item(db, item, who) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": who}))
    return item


def _bound_child(client, key, db, *, bound_only: bool):
    """A child registered on a seat bound to a fresh item. Returns (child, item key)."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "the bound item", ["backend/app/bound.py"])
    kwargs = {"bound_only": True} if bound_only else {}
    out = _ok(_delegate(client, key, item, planner, **kwargs))
    reg = _register(client, key, "child", enrolment_code=out["enrolment_code"],
                    worktree="/w/child", branch="gb/bound",
                    capabilities={"instance": "child"})
    return reg["agent_id"], item, out, reg


# ---- the flag refuses, and its absence does not ------------------------------------------


def test_bound_only_cannot_take_another_review_or_another_item(client, key, db, proj):
    """THE ONE THAT MATTERS. After the bound item is in review, both doors refuse and name it.

    Sabotage: delete the `bound_only_refusal` check in `fleet.claim_review` AND the one in
    `items.claim_next`. The claim_review assertion fails immediately (the review is handed
    over). The claim_next assertion fails only after `release_item`: a live build lease
    raises `AlreadyHolding` first, which looks like a refusal and would hide a missing check.
    """
    other = _in_review_by_somebody_else(client, key, db)
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    held = _stored(db, bound)
    assert held.status == "in_progress" and held.claimed_by == child

    _ok(_mcp(client, key, "update_item",
             {"id": bound, "status": "review", "agent_id": child}))
    held = _stored(db, bound)
    assert held.status == "review"

    with pytest.raises(items_svc.BoundOnlySeat, match=held.key):
        fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    wire = _err(_mcp(client, key, "claim_review", {"agent_id": child}))
    assert wire["code"] == "conflict", wire
    assert held.key in wire["message"]
    assert "bound_only" in wire["message"]

    # The review that was offered is still unclaimed. An empty answer would have leased it.
    assert _stored(db, other).review_claimed_by is None

    released = items_svc.release_item(db, bound, child)
    assert released is not None
    assert released.status == "review" and released.claimed_by is None

    with pytest.raises(items_svc.BoundOnlySeat, match=held.key):
        items_svc.claim_next(db, child, project_id=proj)
    assert _stored(db, ready).claimed_by is None
    assert _stored(db, ready).status == "next"


def _to_review_and_release(client, key, db, child, bound):
    """Move the bound item to review and drop the lease. That is the hole: `claimed_by`
    is what `_holds_bound_item` matches, and review is a status `open_bound_item` ignores."""
    _ok(_mcp(client, key, "update_item",
             {"id": bound, "status": "review", "agent_id": child}))
    # The status move committed on the request's session. This one still has the
    # in_progress row cached, and release_item would then rewrite it to `next`.
    db.expire_all()
    released = items_svc.release_item(db, bound, child)
    assert released is not None
    assert released.status == "review" and released.claimed_by is None
    return released


def test_bound_only_refuses_claim_item_and_claim_cluster_after_release(
        client, key, db, proj):
    """THE BOUNCE. `claim_review` and `claim_next` already refused. `claim_item` is the
    choke point `claim_cluster` and `next_cluster` share, and it did not, so after release
    `claim_cluster` leased the next ready item.

    Sabotage the call, not the helper. Delete the `bound_only_refusal` raise in
    `claim_item`: this test's `claim_item` assertion stops raising and the ready item is
    leased. Delete only the early return in `claim_cluster`: `claim_item` still raises, and
    `claim_cluster` raises out of its member loop instead of returning the reason — the
    dict assertion fails. Both have to stay.
    """
    from app.services import clustering as cluster_svc

    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    held = _stored(db, bound)
    _to_review_and_release(client, key, db, child, bound)

    with pytest.raises(items_svc.BoundOnlySeat, match=held.key):
        items_svc.claim_item(db, ready, child)
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
    assert got["claimed"] is False
    assert held.key in got["reason"] and "bound_only" in got["reason"]
    with pytest.raises(items_svc.BoundOnlySeat, match=held.key):
        cluster_svc.next_cluster(db, child, project_id=proj)

    wire = _ok(_mcp(client, key, "claim_cluster", {"agent_id": child}))
    assert wire["claimed"] is False
    assert held.key in wire["reason"] and "bound_only" in wire["reason"]
    wire_next = _err(_mcp(client, key, "next_cluster", {"agent_id": child}))
    assert wire_next["code"] == "conflict"
    assert held.key in wire_next["message"]

    assert _stored(db, ready).claimed_by is None
    assert _stored(db, ready).status == "next"


def test_bound_only_refuses_the_same_doors_once_the_item_is_done(client, key, db, proj):
    """The lease-dropped opening again, with status done rather than review. `_holds_bound_item`
    and `open_bound_item` both miss a finished item the same way they miss one in review."""
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    held = _stored(db, bound)
    _to_review_and_release(client, key, db, child, bound)
    row = _stored(db, bound)
    row.status = "done"
    db.commit()

    with pytest.raises(items_svc.BoundOnlySeat, match=held.key):
        items_svc.claim_item(db, ready, child)
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
    assert got["claimed"] is False
    assert held.key in got["reason"] and "bound_only" in got["reason"]
    assert _stored(db, ready).claimed_by is None
    assert _stored(db, ready).status == "next"


def test_without_the_flag_claim_cluster_still_claims_after_release(client, key, db, proj):
    """The flag is opt-in. A bound seat without it still takes the next cluster once its own
    item is in review and the lease is dropped — that is what a wave relies on."""
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=False)
    _to_review_and_release(client, key, db, child, bound)

    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
    assert got["claimed"] is True
    assert any(it["stored_id"] == ready for it in got["items"])


def test_without_the_flag_the_same_child_can_review_and_claim(client, key, db, proj):
    """Today's behaviour, pinned so the flag cannot become the default by accident.

    Sabotage the other way: pass bound_only=True in `_bound_child` and claim_review raises.
    """
    other = _in_review_by_somebody_else(client, key, db)
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, out, reg = _bound_child(client, key, db, bound_only=False)
    assert reg["bound_only"] is False
    assert out["bound_only"] is False

    _ok(_mcp(client, key, "update_item",
             {"id": bound, "status": "review", "agent_id": child}))

    got = fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    assert got is not None and got.id == other

    released = items_svc.release_item(db, bound, child)
    assert released.status == "review"
    taken = items_svc.claim_next(db, child, project_id=proj)
    assert taken is not None and taken.id == ready


def test_bound_only_still_reviews_while_its_item_is_in_progress(client, key, db, proj):
    """The gate is 'has left in_progress', not 'is bound'. Mid-build, reviewing is allowed."""
    other = _in_review_by_somebody_else(client, key, db)
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    assert _stored(db, bound).status == "in_progress"

    got = fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    assert got is not None and got.id == other


# ---- a lapsed lease is not a delivered item (GRPH-1101) ----------------------------------


def _lapse(db, child, bound):
    """What `requeue_offline_items` does when presence passes the TTL: `release_item` on the
    holder, which puts the row back in `next` with `claimed_by` empty."""
    db.expire_all()
    released = items_svc.release_item(db, bound, child)
    assert released is not None
    assert released.status == "next" and released.claimed_by is None
    return released


def test_bound_only_reclaims_its_own_item_after_a_lease_lapse(client, key, db, proj):
    """THE INCIDENT. The child's item was requeued while it was still reading, and every
    door then refused it, so it exited having changed nothing.

    Sabotage: restore the old gate (`if bound.status == "in_progress": return None`, refuse
    otherwise, with `claim_item` excusing only the bound item). claim_next raises instead of
    returning the bound item.
    """
    other = _in_review_by_somebody_else(client, key, db)
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    held = _stored(db, bound)
    _lapse(db, child, bound)

    # claim_next resolves to the bound item, even though `ready` is also ready.
    got = items_svc.claim_next(db, child, project_id=proj)
    assert got is not None and got.id == bound
    row = _stored(db, bound)
    assert row.status == "in_progress" and row.claimed_by == child
    assert _stored(db, ready).claimed_by is None

    # And by name, through claim_item — after a second lapse.
    _lapse(db, child, bound)
    assert items_svc.claim_item(db, bound, child) is not None
    assert _stored(db, bound).claimed_by == child

    # Nothing else while the item is back in next: not another item, not a review.
    _lapse(db, child, bound)
    with pytest.raises(items_svc.BoundOnlySeat, match=held.key) as e:
        items_svc.claim_item(db, ready, child)
    assert "Reclaim" in str(e.value)
    with pytest.raises(items_svc.BoundOnlySeat, match="Reclaim"):
        fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
    assert got["claimed"] is False and held.key in got["reason"]
    assert _stored(db, ready).claimed_by is None
    assert _stored(db, other).review_claimed_by is None

    # Over the wire: claim_next hands it back.
    wire = _ok(_mcp(client, key, "claim_next", {"agent_id": child}))
    assert held.key in str(wire)
    assert _stored(db, bound).claimed_by == child


def test_bound_only_reclaim_respects_a_skip(client, key, db, proj):
    """A seat that declines its own item gets the refusal, not somebody else's item."""
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    _lapse(db, child, bound)
    with pytest.raises(items_svc.BoundOnlySeat, match="Reclaim"):
        items_svc.claim_next(db, child, project_id=proj, skip=[bound])
    assert _stored(db, ready).claimed_by is None


def test_bound_only_refuses_and_names_the_holder_once_another_agent_has_it(
        client, key, db, proj):
    """After the lapse somebody else took the item. The seat may not take it, or anything
    else, and the reason names who has it."""
    other = _in_review_by_somebody_else(client, key, db)
    ready = _item(client, key, "still ready", ["backend/app/ready.py"])
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    _lapse(db, child, bound)
    thief = _agent(client, key, "thief", worktree="/w/thief", branch="gb/thief",
                   capabilities={"instance": "thief"})
    assert items_svc.claim_item(db, bound, thief) is not None

    with pytest.raises(items_svc.BoundOnlySeat, match=thief):
        items_svc.claim_next(db, child, project_id=proj)
    with pytest.raises(items_svc.BoundOnlySeat, match=thief):
        fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    with pytest.raises(items_svc.BoundOnlySeat, match=thief):
        items_svc.claim_item(db, ready, child)
    assert items_svc.claim_item(db, bound, child) is None
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
    assert got["claimed"] is False and thief in got["reason"]
    wire = _err(_mcp(client, key, "claim_review", {"agent_id": child}))
    assert wire["code"] == "conflict" and thief in wire["message"]

    assert _stored(db, bound).claimed_by == thief
    assert _stored(db, ready).claimed_by is None
    assert _stored(db, other).review_claimed_by is None


def test_bound_only_reclaim_is_not_offered_once_the_item_is_delivered(client, key, db, proj):
    """Review is delivered work. claim_next does not hand the bound item back, it refuses."""
    child, bound, _out, _reg = _bound_child(client, key, db, bound_only=True)
    _to_review_and_release(client, key, db, child, bound)
    assert items_svc.reclaimable_bound_item(db, child) is None
    with pytest.raises(items_svc.BoundOnlySeat, match="left in_progress"):
        items_svc.claim_next(db, child, project_id=proj)
    assert _stored(db, bound).status == "review"


# ---- mint, reissue, and the wires --------------------------------------------------------


def test_delegate_reply_and_register_echo_the_flag(client, key, db):
    child, _bound, out, reg = _bound_child(client, key, db, bound_only=True)
    assert out["bound_only"] is True
    assert reg["bound_only"] is True
    assert _seat(db).bound_only is True
    again = _register(client, key, "plain")
    assert again["bound_only"] is False
    # The child already registered; a second registration is a new agent. Re-read the seat
    # through the first child's reply by minting nothing — the stored row is the evidence.
    assert items_svc.bound_only_seat(db, child) is True


def test_bound_only_without_a_seat_writes_nothing(client, key, db):
    planner = _agent(client, key, "planner")
    item = _item(client, key, "unbound")
    e = _err(_mcp(client, key, "delegate", {
        "id": item, "lane": "backend", "tier": "cheap", "agent_id": planner,
        "seat": False, "bound_only": True}))
    assert e["code"] == "validation", e
    assert "seat=true" in e["message"]
    db.expire_all()
    assert db.scalars(select(Delegation)).all() == []
    assert db.scalars(select(Enrolment)).all() == []


def test_a_fresh_seat_defaults_the_flag_off(db, proj):
    row, _code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker")
    assert row.bound_only is False


def test_bound_only_without_an_item_is_refused(db, proj):
    with pytest.raises(ValueError, match="item"):
        fleet_svc.issue_enrolment(db, project_id=proj, role="worker", bound_only=True)
    db.expire_all()
    assert db.scalars(select(Enrolment)).all() == []


def test_bound_only_and_review_only_together_are_refused(db, proj):
    item = Item(id="it-b", project_id=proj, number=1, title="x", status="next")
    db.add(item)
    db.commit()
    with pytest.raises(ValueError, match="review-only"):
        fleet_svc.issue_enrolment(db, project_id=proj, role="worker", item_id=item.id,
                                  review_only=True, bound_only=True)


def test_reissue_carries_bound_only_and_the_item(db, proj):
    """A crash must not widen the seat. An ordinary bound seat still drops the item."""
    item = Item(id="it-b", project_id=proj, number=1, title="x", status="next")
    db.add(item)
    db.commit()
    row, _code = fleet_svc.issue_enrolment(
        db, project_id=proj, role="worker", item_id=item.id, delegation_id="dlg_1",
        bound_only=True)
    fresh, _code = fleet_svc.reissue_enrolment(db, enrolment_id=row.id)
    assert fresh.bound_only is True
    assert fresh.item_id == item.id
    assert fresh.delegation_id == "dlg_1"
    assert fresh.reissued_from == row.id

    plain, _code = fleet_svc.issue_enrolment(
        db, project_id=proj, role="worker", item_id=item.id, bound_only=False)
    widened, _code = fleet_svc.reissue_enrolment(db, enrolment_id=plain.id)
    assert widened.bound_only is False
    assert widened.item_id is None


def test_mint_enrolment_bound_only_needs_an_item_and_echoes_the_flag(
        client, auth, key, proj, db):
    planner = _planner(client, auth, key, proj)
    bare = _err(_mcp(client, key, "mint_enrolment",
                     {"agent_id": planner, "role": "worker", "bound_only": True}))
    assert bare["code"] == "validation", bare

    item = _item(client, key, "minted bound")
    both = _err(_mcp(client, key, "mint_enrolment", {
        "agent_id": planner, "role": "worker", "bound_only": True, "review_only": True,
        "item": item}))
    assert both["code"] == "validation", both

    minted = _ok(_mcp(client, key, "mint_enrolment", {
        "agent_id": planner, "role": "worker", "bound_only": True, "item": item}))
    assert minted["bound_only"] is True
    seat = _seat(db)
    assert seat.bound_only is True
    assert seat.item_id == _stored(db, item).id
    registered = _register(client, key, "minted-child",
                           enrolment_code=minted["enrolment_code"],
                           capabilities={"instance": "minted"})
    assert registered["bound_only"] is True


def test_mint_bound_only_writes_nothing_when_the_areas_are_held(
        client, auth, key, proj, db):
    planner = _planner(client, auth, key, proj)
    holder = _agent(client, key, "holder")
    _item(client, key, "held", ["backend/app/services/x.py"])
    assert fleet_svc.claim_cluster(db, agent_id=holder, project_id=proj)["claimed"]
    second = _item(client, key, "colliding", ["backend/app/services/x.py"])
    before = len(db.scalars(select(Enrolment)).all())
    e = _err(_mcp(client, key, "mint_enrolment", {
        "agent_id": planner, "role": "worker", "bound_only": True, "item": second}))
    assert e["code"] == "conflict" and holder in e["message"]
    db.expire_all()
    assert len(db.scalars(select(Enrolment)).all()) == before


def test_bound_only_is_fleet_only_so_the_ceiling_does_not_move():
    """The property is advertised to the fleet tier. The base manifest, which the ceiling
    measures, does not carry it."""
    for name in ("delegate", "mint_enrolment"):
        base = next(t for t in TOOLS if t["name"] == name)
        assert "bound_only" not in base["inputSchema"]["properties"]
        assert "bound_only" in _FLEET_ONLY_ARGS[name]
    assert "item" in _FLEET_ONLY_ARGS["mint_enrolment"]
    assert "item" not in next(
        t for t in TOOLS if t["name"] == "mint_enrolment")["inputSchema"]["properties"]
