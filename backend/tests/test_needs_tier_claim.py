"""GRPH-1647. A bounce that names `needs_tier` has to bind the claim paths, not only
`gbfleet until`. An undelegated agent whose declared tier is below that tier is skipped
or refused, and the response names the tier. An agent that declared no tier is
`undeclared`: the mismatch rule does not refuse it, and an empty claim would.
"""
from __future__ import annotations

import pytest

from app.models import Item
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


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Needs tier"}, headers=auth).json()["id"]


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


def _agent(client, key, label, **capabilities) -> str:
    caps = {"vendor": "xai", "model": "grok-4.7", **capabilities}
    return _ok(_mcp(client, key, "register_agent",
                    {"branch": "gb/test", "label": label, "capabilities": caps}))["agent_id"]


def _item(client, key, title, touchpoints=None) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next",
        "touchpoints": touchpoints or ["backend/app/services/fleet.py"]}))["id"]


def _stored(db, item_key) -> Item:
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


def _bounce_needs(client, key, db, item, tier):
    """Put `item` through a real bounce that names `tier`. It comes back unpinned."""
    author = _agent(client, key, f"author-{item}", tier="cheap")
    assert items_svc.claim_item(db, item, author) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": author}))
    reviewer = _agent(client, key, f"rev-{item}", tier="frontier", vendor="other")
    _ok(_mcp(client, key, "bounce", {
        "id": item, "agent_id": reviewer, "needs_tier": tier,
        "reason": "the cheap tier cannot produce the receipt"}))
    stored = _stored(db, item)
    assert stored.status == "next" and stored.claimed_by is None
    assert fleet_svc.bounce_needs_tier(stored) == tier
    return stored


def test_a_cheap_claim_next_is_skipped_and_names_needs_tier(client, key, db):
    """The claim the bug names. Sabotage: drop the tier skip in `claim_next` — this
    agent takes the item and `needs_tier` is absent."""
    item = _item(client, key, "frontier work")
    _bounce_needs(client, key, db, item, "frontier")
    cheap = _agent(client, key, "cheap-next", tier="cheap")

    refused = _ok(_mcp(client, key, "claim_next", {"agent_id": cheap}))
    assert refused["claimed"] is False and refused["item"] is None
    assert refused["needs_tier"] == [{
        "id": item, "needs_tier": "frontier", "declared_tier": "cheap"}]
    assert _stored(db, item).status == "next" and _stored(db, item).claimed_by is None

    # The same empty answer from the neighbourhood path, not a bare zero.
    neighbourhood = _ok(_mcp(client, key, "next_cluster", {"agent_id": cheap}))
    assert neighbourhood["claimed"] == 0
    assert neighbourhood["needs_tier"] == refused["needs_tier"]


def test_claim_next_skips_the_tier_item_and_takes_the_next(client, key, db):
    """A skip, not a refusal of the whole board. The plain item is still claimable."""
    plain = _item(client, key, "plain", touchpoints=["backend/app/services/items.py"])
    held = _item(client, key, "held for frontier", touchpoints=["backend/app/services/fleet.py"])
    _bounce_needs(client, key, db, held, "frontier")
    cheap = _agent(client, key, "cheap-skip", tier="cheap")

    got = _ok(_mcp(client, key, "claim_next", {"agent_id": cheap}))
    assert got["claimed"] is True and got["item"]["id"] == plain
    assert "needs_tier" not in got
    assert _stored(db, held).status == "next" and _stored(db, held).claimed_by is None


def test_a_frontier_agent_claims_what_the_bounce_named(client, key, db):
    """At the named tier the claim lands, and the reply does not pretend it was held."""
    item = _item(client, key, "for frontier")
    _bounce_needs(client, key, db, item, "frontier")
    frontier = _agent(client, key, "frontier-ok", tier="frontier")

    got = _ok(_mcp(client, key, "claim_next", {"agent_id": frontier}))
    assert got["claimed"] is True and got["item"]["id"] == item
    assert "needs_tier" not in got
    assert _stored(db, item).claimed_by == frontier


def test_a_tier_that_is_not_below_still_claims(client, key, db):
    """`below` is strict. The named tier itself, and a tier above it, both claim.
    Sabotage: compare with `!=` — the frontier agent is refused a cheap-needed item."""
    same = _item(client, key, "needs cheap")
    _bounce_needs(client, key, db, same, "cheap")
    cheap = _agent(client, key, "cheap-same", tier="cheap")
    got = _ok(_mcp(client, key, "claim_next", {"agent_id": cheap}))
    assert got["claimed"] is True and got["item"]["id"] == same

    above = _item(client, key, "also needs cheap", touchpoints=["web/src/a.tsx"])
    _bounce_needs(client, key, db, above, "cheap")
    frontier = _agent(client, key, "frontier-above", tier="frontier")
    got = _ok(_mcp(client, key, "claim_next", {"agent_id": frontier}))
    assert got["claimed"] is True and got["item"]["id"] == above


def test_an_undeclared_tier_is_not_refused(client, key, db):
    """D8. No tier, a blank, and the literal `undeclared` are not below anything.
    Sabotage: treat a missing tier as cheap — each of these comes back unclaimed."""
    for label, caps in (
        ("no-field", {}),
        ("blank", {"tier": "  "}),
        ("word", {"tier": "undeclared"}),
    ):
        item = _item(client, key, label, touchpoints=[f"backend/app/{label}.py"])
        _bounce_needs(client, key, db, item, "frontier")
        who = _agent(client, key, label, **caps)
        got = _ok(_mcp(client, key, "claim_next", {"agent_id": who}))
        assert got["claimed"] is True and got["item"]["id"] == item, (label, got)
        assert "needs_tier" not in got
        assert _stored(db, item).claimed_by == who


def test_claim_item_refuses_a_lower_tier_and_names_it(client, key, db):
    """A named claim raises. None would read as a lost race. Sabotage: return None
    instead of raising — this passes and the caller reports the item held."""
    item = _item(client, key, "named")
    _bounce_needs(client, key, db, item, "frontier")
    cheap = _agent(client, key, "cheap-named", tier="cheap")

    with pytest.raises(items_svc.TierBelowNeeds, match=r"needs_tier=frontier") as raised:
        items_svc.claim_item(db, item, cheap)
    assert raised.value.needs_tier == "frontier"
    assert raised.value.declared_tier == "cheap"
    stored = _stored(db, item)
    assert stored.status == "next" and stored.claimed_by is None

    # The same agent, declaring nothing, is not refused.
    open_agent = _agent(client, key, "undeclared-named")
    claimed = items_svc.claim_item(db, item, open_agent)
    assert claimed is not None and claimed.claimed_by == open_agent


def test_claim_cluster_skips_and_names_needs_tier(client, key, db):
    """The cluster reply names the tier. Sabotage: let `claim_item` raise out of the
    loop — the call 500s. Or drop the raise — the cheap agent takes the item."""
    item = _item(client, key, "cluster")
    _bounce_needs(client, key, db, item, "frontier")
    cheap = _agent(client, key, "cheap-cluster", tier="cheap")

    got = _ok(_mcp(client, key, "claim_cluster", {"agent_id": cheap}))
    assert got["claimed"] is False
    assert "needs_tier=frontier" in got["reason"]
    assert got["needs_tier"] == [{
        "id": item, "needs_tier": "frontier", "declared_tier": "cheap"}]
    assert _stored(db, item).status == "next" and _stored(db, item).claimed_by is None


def test_next_cluster_does_not_take_a_higher_tier_neighbour(client, key, db):
    """The seed is claimable. The neighbour the tier cannot take must not fail the call
    and must not be claimed. Sabotage: drop `TierBelowNeeds` from the neighbour handler —
    the call errors and the seed's claim is the only thing that landed."""
    plain = _item(client, key, "seed", touchpoints=["backend/app/services/items.py"])
    neighbour = _item(client, key, "neighbour", touchpoints=["backend/app/services/items.py"])
    _bounce_needs(client, key, db, neighbour, "frontier")
    cheap = _agent(client, key, "cheap-neighbour", tier="cheap")

    got = _ok(_mcp(client, key, "next_cluster", {"agent_id": cheap}))
    assert got["claimed"] == 1
    assert [row["id"] for row in got["cluster"]] == [plain]
    assert "needs_tier" not in got
    assert _stored(db, neighbour).status == "next"
    assert _stored(db, neighbour).claimed_by is None


def test_a_cheap_bound_seat_does_not_claim_at_registration(client, key, db):
    """Registration claims through `claim_item`. A cheap seat is told the tier, and the
    item stays unclaimed. Sabotage: drop the `TierBelowNeeds` handler in
    `_claim_bound_seat` — registration 500s."""
    planner = _agent(client, key, "planner", tier="frontier")
    item = _item(client, key, "bound")
    _bounce_needs(client, key, db, item, "frontier")
    minted = _ok(_mcp(client, key, "delegate", {
        "id": item, "lane": "backend", "tier": "cheap", "agent_id": planner, "seat": True}))
    reg = _ok(_mcp(client, key, "register_agent", {
        "branch": "gb/test", "label": "cheap-seat",
        "enrolment_code": minted["enrolment_code"],
        "capabilities": {"tier": "cheap", "vendor": "xai", "model": "grok-4.7"}}))
    assert reg["assigned"]["state"] == "taken"
    assert "needs_tier=frontier" in reg["assigned"]["reason"]
    assert reg["assigned"]["item"] == item
    # The supervisor reads the roster, not this reply (GRPH-1292).
    rows = {a["id"]: a for a in _ok(_mcp(client, key, "fleet_status", {}))["agents"]}
    assert "needs_tier=frontier" in rows[reg["agent_id"]]["assigned"]["reason"]
    stored = _stored(db, item)
    assert stored.status == "next" and stored.claimed_by is None


def test_an_undeclared_bound_seat_still_claims(client, key, db):
    """The registration caller does not refuse an unknown. Sabotage: treat a missing
    tier as below — `assigned.state` is `taken` and the item stays `next`."""
    planner = _agent(client, key, "planner-u", tier="frontier")
    item = _item(client, key, "bound-undeclared")
    _bounce_needs(client, key, db, item, "frontier")
    minted = _ok(_mcp(client, key, "delegate", {
        "id": item, "lane": "backend", "tier": "frontier", "agent_id": planner, "seat": True}))
    reg = _ok(_mcp(client, key, "register_agent", {
        "branch": "gb/test", "label": "undeclared-seat",
        "enrolment_code": minted["enrolment_code"],
        "capabilities": {"vendor": "xai", "model": "grok-4.7"}}))
    assert reg["assigned"]["state"] == "claimed" and reg["assigned"]["reason"] is None
    assert _stored(db, item).claimed_by == reg["agent_id"]


def test_an_empty_board_does_not_grow_a_needs_tier_key(client, key):
    """The key is present only when a tier was the reason. An always-present key would
    make an empty board look held."""
    who = _agent(client, key, "idle", tier="cheap")
    nothing = _ok(_mcp(client, key, "claim_next", {"agent_id": who}))
    assert nothing["claimed"] is False and nothing["item"] is None
    assert "needs_tier" not in nothing
    cluster = _ok(_mcp(client, key, "claim_cluster", {"agent_id": who}))
    assert cluster["claimed"] is False and "needs_tier" not in cluster
