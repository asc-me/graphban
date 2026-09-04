"""PRD-35 PR 1 — delegation as a ledger fact: the brief, the record, the requested tier.

Each test names the acceptance criterion it pins (§7) and, where it matters, the sabotage
run against it. Rows are read through a session opened after the request so the app's own
commit is what is being inspected.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Agent, AgentCall, Delegation, Item
from app.services import delegation as dsvc
from app.services import fleet as fleet_svc
from app.services import items as items_svc

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


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
    return client.post("/api/projects", json={"name": "Delegation"}, headers=auth).json()["id"]


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


def _item(client, key, title="delegate me", touchpoints=None, status="next", **kw) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": status, "touchpoints": touchpoints or ["backend/app/x.py"],
        **kw}))["id"]


def _details(client, key, item_id) -> dict:
    return _ok(_mcp(client, key, "get_item_details", {"id": item_id}))


def _delegate(client, key, item_id, who, lane="backend", tier="cheap", **kw):
    return _mcp(client, key, "delegate", {"id": item_id, "lane": lane, "tier": tier,
                                          "agent_id": who, **kw})


def _rows(db, item_key=None) -> list[Delegation]:
    db.expire_all()
    stmt = select(Delegation).order_by(Delegation.created_at, Delegation.id)
    return db.scalars(stmt).all()


def _stored(db, item_key) -> Item:
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


# ---- 1–3, 26, 27: the brief ---------------------------------------------------------------

def test_a_fresh_item_briefs_cheap_with_basis_none(client, key):
    """1. Sabotage: drop `basis` from `tier_for` — this fails on the key, not the value."""
    item = _item(client, key)
    brief = _details(client, key, item)["brief"]
    assert brief["tier"] == {"value": "cheap", "basis": "none"}
    assert brief["previous"] is None and brief["attempts"] == []


def test_lane_is_decided_by_touchpoints_and_names_them(client, key):
    """2."""
    web = _item(client, key, touchpoints=["web/src/features/live/LiveView.tsx"])
    lane = _details(client, key, web)["brief"]["lane"]
    assert lane["value"] == "frontend"
    assert lane["basis"] == ["web/src/features/live/LiveView.tsx"]

    mixed = _item(client, key, touchpoints=["web/src/lib/api.ts", "backend/app/routers/live.py"])
    lane = _details(client, key, mixed)["brief"]["lane"]
    assert lane["value"] == "mixed"
    assert set(lane["basis"]) == {"web/src/lib/api.ts", "backend/app/routers/live.py"}

    assert _details(client, key, _item(client, key))["brief"]["lane"]["value"] == "backend"


def test_the_text_carries_every_field_it_is_derived_from(client, key):
    """3. Sabotage: drop the touchpoint line from `_text` — this fails."""
    item = _item(client, key, touchpoints=["backend/app/mcp_server.py", "docs/mcp.md"])
    _ok(_mcp(client, key, "add_memory", {"text": "lesson one about the dispatcher",
                                          "scope": "item", "item_id": item}))
    brief = _details(client, key, item)["brief"]
    assert brief["checklist"] == "mcp_tool"
    assert len(brief["lessons"]) == 1
    text = brief["text"]
    assert item in text
    for tp in brief["touchpoints"]:
        assert tp in text
    assert "mcp_tool" in text
    assert brief["lessons"][0]["id"] in text


def test_the_text_carries_no_suggestion(client, key):
    """26 / D16. Sabotage: append the tier suggestion to the text — this fails."""
    item = _item(client, key, touchpoints=["web/src/a.tsx"])
    text = _details(client, key, item)["brief"]["text"]
    # The task class ("frontend") is a field of its own and may appear; the LANE and TIER
    # suggestions may not, in any spelling.
    for banned in ("lane", "tier", "Suggest", "cheap", "frontier"):
        assert banned.lower() not in text.lower(), banned


def test_the_brief_caps_summary_and_lessons(client, key):
    """27 / D17."""
    long_first = "x" * 700 + "\n\nsecond paragraph must not appear"
    item = _item(client, key, description=long_first)
    for i in range(6):
        _ok(_mcp(client, key, "add_memory", {"text": f"lesson number {i}",
                                              "scope": "item", "item_id": item}))
    brief = _details(client, key, item)["brief"]
    assert len(brief["summary"]) == dsvc.SUMMARY_MAX
    assert "second paragraph" not in brief["summary"]
    assert "second paragraph" not in brief["text"]
    assert len(brief["lessons"]) == dsvc.LESSONS_MAX
    shown = {l["text"] for l in brief["lessons"]}
    missing = {f"lesson number {i}" for i in range(6)} - shown
    assert len(missing) == 1
    assert next(iter(missing)) not in brief["text"]


# ---- 4–6, 24, 25: the write ---------------------------------------------------------------

def test_delegate_requires_tier_and_lane(client, key):
    """4 / D5: no default, because a default is the server choosing."""
    item = _item(client, key)
    me = _agent(client, key, "planner")
    assert _err(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "agent_id": me}))["code"] == "validation"
    assert _err(_mcp(client, key, "delegate", {"id": item, "tier": "cheap", "agent_id": me}))["code"] == "validation"


def test_delegate_refuses_a_blocked_item_and_names_the_blocker(client, key):
    """5a."""
    item = _item(client, key)
    _ok(_mcp(client, key, "update_item", {"id": item, "blocker": "waiting on the schema"}))
    me = _agent(client, key, "planner")
    e = _err(_delegate(client, key, item, me))
    assert e["code"] == "conflict" and "waiting on the schema" in e["message"]


def test_delegate_refuses_when_another_delegation_is_open(client, key):
    """5b / 25b: refused with the open one's id and age."""
    item = _item(client, key)
    a = _agent(client, key, "planner-a")
    b = _agent(client, key, "planner-b")
    first = _ok(_delegate(client, key, item, a))
    e = _err(_delegate(client, key, item, b))
    assert e["code"] == "conflict"
    assert first["delegation_id"] in e["message"]
    assert "age_seconds=" in e["message"]


def test_delegate_refuses_the_holder(client, key):
    """6."""
    item = _item(client, key)
    me = _agent(client, key, "worker")
    assert _ok(_mcp(client, key, "claim_next", {"agent_id": me}))["item"]["id"] == item
    e = _err(_delegate(client, key, item, me))
    assert e["code"] == "conflict" and "you hold" in e["message"]


def test_delegate_needs_a_registered_caller(client, key):
    item = _item(client, key)
    e = _err(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap"}))
    assert e["code"] == "validation" and "register" in e["message"]


def test_the_owner_withdraws_by_redelegating(client, key, db):
    """25a / D14."""
    item = _item(client, key)
    me = _agent(client, key, "planner")
    first = _ok(_delegate(client, key, item, me))
    assert first["withdrew"] is None
    second = _ok(_delegate(client, key, item, me, tier="frontier"))
    assert second["withdrew"] == first["delegation_id"]
    assert second["delegation_id"] != first["delegation_id"]
    rows = {r.id: r for r in _rows(db)}
    assert rows[first["delegation_id"]].closed_reason == "withdrawn"
    assert dsvc.state(rows[first["delegation_id"]]) == "closed"
    assert dsvc.state(rows[second["delegation_id"]]) == "open"


# ---- 7–10: the link ------------------------------------------------------------------------

def _child(client, key, parent, label="child", **caps) -> str:
    return _agent(client, key, label, parent_agent_id=parent, capabilities=caps)


def test_a_declared_child_links_and_a_stranger_supersedes(client, key, db):
    """7 / D7."""
    planner = _agent(client, key, "planner")
    # the child
    item = _item(client, key, "one")
    d = _ok(_delegate(client, key, item, planner))
    child = _child(client, key, planner, model="haiku", tier="cheap")
    assert items_svc.claim_item(db, item, child) is not None
    row = db.get(Delegation, d["delegation_id"]); db.refresh(row)
    assert row.agent_id == child and row.linked_by == "parent"
    assert dsvc.state(row) == "claimed"

    # a stranger
    item2 = _item(client, key, "two")
    d2 = _ok(_delegate(client, key, item2, planner))
    stranger = _agent(client, key, "stranger")
    assert items_svc.claim_item(db, item2, stranger) is not None
    row2 = db.get(Delegation, d2["delegation_id"]); db.refresh(row2)
    assert row2.agent_id is None
    assert row2.closed_reason == "superseded" and row2.closed_by == stranger
    assert dsvc.state(row2) == "closed"

    # the delegator itself — exactly the failure the record exists to show
    item3 = _item(client, key, "three")
    d3 = _ok(_delegate(client, key, item3, planner))
    assert items_svc.claim_item(db, item3, planner) is not None
    row3 = db.get(Delegation, d3["delegation_id"]); db.refresh(row3)
    assert row3.closed_reason == "superseded" and row3.closed_by == planner


def test_a_child_on_a_seat_the_delegator_minted_links_by_seat(client, key, proj, db):
    """7 / D7: a SPAWNED process must not declare a parent (that field feeds review
    independence), so its lineage is the seat. Sabotage: drop the seat branch of
    `lineage` — this fails."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner))
    _, code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker", minted_by=planner)
    child = _agent(client, key, "seat-child", enrolment_code=code)
    assert db.get(Agent, child).parent_agent_id is None
    assert items_svc.claim_item(db, item, child) is not None
    row = db.get(Delegation, d["delegation_id"]); db.refresh(row)
    assert row.agent_id == child and row.linked_by == "seat"


@pytest.mark.parametrize("path", ["claim_next", "claim_item", "claim_cluster", "next_cluster"])
def test_every_claim_path_links(client, key, proj, db, path):
    """8. One write point (`_try_claim`), four doors. Sabotage: skip `on_claim` for one door
    by claiming through a raw UPDATE — the door under test then fails."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner))
    child = _child(client, key, planner)
    if path == "claim_next":
        assert _ok(_mcp(client, key, "claim_next", {"agent_id": child}))["item"]["id"] == item
    elif path == "claim_item":
        assert items_svc.claim_item(db, item, child) is not None
    elif path == "claim_cluster":
        got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)
        assert got["claimed"], got
    else:
        from app.services import clustering
        assert clustering.next_cluster(db, child, project_id=proj)
    row = db.get(Delegation, d["delegation_id"]); db.refresh(row)
    assert row.agent_id == child, path


def test_declared_model_and_tier_are_copied_and_undeclared_is_a_word(client, key, db):
    """9 / D8."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "declared")
    d = _ok(_delegate(client, key, item, planner))
    child = _child(client, key, planner, model="qwen3.6:35b", tier="cheap")
    items_svc.claim_item(db, item, child)
    row = dsvc.row_dict(db.get(Delegation, d["delegation_id"]))
    assert row["declared_model"] == "qwen3.6:35b" and row["declared_tier"] == "cheap"
    assert row["mismatch"] is False

    item2 = _item(client, key, "undeclared")
    d2 = _ok(_delegate(client, key, item2, planner))
    bare = _child(client, key, planner, label="bare")
    items_svc.claim_item(db, item2, bare)
    row2 = dsvc.row_dict(db.get(Delegation, d2["delegation_id"]))
    assert row2["declared_tier"] == "undeclared" and row2["declared_model"] is None
    assert row2["mismatch"] is False, "undeclared is neither a match nor a mismatch"


def test_a_mismatch_is_a_row_not_a_refusal(client, key, db):
    """10 / D8."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner, tier="cheap"))
    child = _child(client, key, planner, model="opus-5", tier="frontier")
    assert items_svc.claim_item(db, item, child) is not None
    row = dsvc.row_dict(db.get(Delegation, d["delegation_id"]))
    assert row["requested_tier"] == "cheap" and row["declared_tier"] == "frontier"
    assert row["mismatch"] is True and row["state"] == "claimed"


# ---- 11, 28: the clock --------------------------------------------------------------------

def test_open_becomes_expired_when_the_clock_moves(client, key, db):
    """11 / D9."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner))
    row = db.get(Delegation, d["delegation_id"])
    created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    assert dsvc.state(row, now=created + timedelta(seconds=1)) == "open"
    assert dsvc.state(row, now=created + timedelta(seconds=row.lease_seconds + 1)) == "expired"


def test_the_lease_is_copied_at_write(client, key, db, monkeypatch):
    """28 / D18. Sabotage: read `DEFAULT_LEASE_SECONDS` in `state` — this fails."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner))
    row = db.get(Delegation, d["delegation_id"])
    assert row.lease_seconds == items_svc.DEFAULT_LEASE_SECONDS
    monkeypatch.setattr(items_svc, "DEFAULT_LEASE_SECONDS", 5)
    created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    assert dsvc.state(row, now=created + timedelta(seconds=60)) == "open"


def test_an_expired_delegation_can_be_followed_by_another_delegator(client, key, db):
    planner = _agent(client, key, "planner-a")
    other = _agent(client, key, "planner-b")
    item = _item(client, key)
    d = _ok(_delegate(client, key, item, planner))
    row = db.get(Delegation, d["delegation_id"])
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=row.lease_seconds + 5)
    db.commit()
    second = _ok(_delegate(client, key, item, other))
    assert second["withdrew"] is None
    db.refresh(row)
    assert dsvc.state(row) == "expired", "the spawn that never came stays on the record"


# ---- 12–14, 24: outcomes and re-delegation --------------------------------------------------

def _linked(client, key, db, planner, title="work", **caps):
    item = _item(client, key, title)
    d = _ok(_delegate(client, key, item, planner))
    child = _child(client, key, planner, label=f"child-{title}", instance=title, **caps)
    assert items_svc.claim_item(db, item, child) is not None
    return item, d["delegation_id"], child


def test_sign_off_finishes_the_attempt(client, key, db):
    """12a."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner)
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "rev"})
    _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                        "evidence": [{"kind": "note", "detail": "read it"}]}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "signed_off" and dsvc.state(row) == "finished"


def test_bounce_release_and_block_finish_the_attempt(client, key, db):
    """12b–d. Sabotage: remove the hook from one transition — that case fails."""
    planner = _agent(client, key, "planner")

    item, did, child = _linked(client, key, db, planner, "bounced")
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, "reviewer", capabilities={"instance": "rev"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "tests missing"}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "bounced"

    item, did, child = _linked(client, key, db, planner, "released")
    _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": child}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "released"

    item, did, child = _linked(client, key, db, planner, "blocked")
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "blocked", "agent_id": child,
                                           "blocker": "needs a decision"}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "blocked"


def test_a_lost_lease_reads_released_when_someone_else_takes_the_item(client, key, db):
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner)
    stored = _stored(db, item)
    stored.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=items_svc.DEFAULT_LEASE_SECONDS + 5)
    db.commit()
    stranger = _agent(client, key, "stranger")
    assert items_svc.claim_item(db, item, stranger) is not None
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "released" and row.agent_id == child


def _bounced(client, key, db, planner, title="bounce-me", **caps):
    item, did, child = _linked(client, key, db, planner, title, **caps)
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"reviewer-{title}", capabilities={"instance": f"rev-{title}"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "flaky test"}))
    return item, did, child


def test_a_pin_refuses_a_stranger_and_admits_the_parent(client, key, db):
    """24 / D15."""
    planner = _agent(client, key, "planner")
    item, did, child = _bounced(client, key, db, planner)
    other = _agent(client, key, "other-planner")
    e = _err(_delegate(client, key, item, other))
    assert e["code"] == "conflict" and "pinned_until=" in e["message"]
    assert _ok(_delegate(client, key, item, planner, tier="frontier"))["state"] == "open"


def test_redelegation_after_a_bounce_carries_the_history(client, key, db):
    """13 / D10."""
    planner = _agent(client, key, "planner")
    item, did, child = _bounced(client, key, db, planner, model="haiku", tier="cheap")
    brief = _details(client, key, item)["brief"]
    assert brief["tier"] == {"value": "frontier", "basis": "bounced"}
    assert brief["previous"]["requested_tier"] == "cheap"
    assert brief["previous"]["declared_model"] == "haiku"
    assert brief["previous"]["outcome"] == "bounced"
    assert brief["previous"]["bounce_reason"] == "flaky test"
    assert brief["pinned"]["to"] == child
    assert "flaky test" in brief["text"]

    # two more attempts, the second by the parent's next child
    second = _ok(_delegate(client, key, item, planner, tier="frontier"))
    stored = _stored(db, item)
    stored.bounce_pinned_until = None; stored.bounce_pinned_to = None
    db.commit()
    child2 = _child(client, key, planner, label="child-2", model="opus-5", tier="frontier")
    assert items_svc.claim_item(db, item, child2) is not None
    _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": child2}))
    third = _ok(_delegate(client, key, item, planner, tier="cheap"))
    brief = _details(client, key, item)["brief"]
    assert [a["requested_tier"] for a in brief["attempts"]] == ["cheap", "frontier"]
    assert [a["outcome"] for a in brief["attempts"]] == ["bounced", "released"]
    assert brief["previous"]["declared_model"] == "opus-5"
    assert brief["tier"] == {"value": "frontier", "basis": "released"}


def test_the_suggestion_is_not_a_default(client, key, db):
    """14 / D5."""
    planner = _agent(client, key, "planner")
    item, did, child = _bounced(client, key, db, planner)
    assert _details(client, key, item)["brief"]["tier"]["value"] == "frontier"
    d = _ok(_delegate(client, key, item, planner, tier="cheap"))
    assert db.get(Delegation, d["delegation_id"]).requested_tier == "cheap"


# ---- 15 (payload), 17, 18, 20: surfaces and guards -------------------------------------------

def test_the_live_payload_carries_delegations_per_delegator(client, key, auth, proj, db):
    """15, payload half / D11, D19. Sabotage: key the board by item instead of delegator —
    planner-b's row leaks into planner-a's and this fails."""
    a = _agent(client, key, "planner-a")
    b = _agent(client, key, "planner-b")
    idle = _agent(client, key, "idle")
    i1 = _item(client, key, "one"); i2 = _item(client, key, "two"); i3 = _item(client, key, "three")
    _ok(_delegate(client, key, i1, a)); _ok(_delegate(client, key, i2, a))
    _ok(_delegate(client, key, i3, b))
    child = _child(client, key, a, model="haiku", tier="cheap")
    items_svc.claim_item(db, i1, child)

    board = client.get(f"/api/live?project_id={proj}", headers=auth).json()
    agents = {ag["id"]: ag for g in board["users"] for ag in g["agents"]}
    assert agents[idle]["delegations"] is None
    da = agents[a]["delegations"]
    assert da["open"] == 1 and da["claimed"] == 1
    assert da["oldest_open_seconds"] is not None
    assert {r["item"] for r in da["rows"]} == {i1, i2}
    assert agents[b]["delegations"]["open"] == 1
    assert {r["item"] for r in agents[b]["delegations"]["rows"]} == {i3}
    claimed = next(r for r in da["rows"] if r["state"] == "claimed")
    assert claimed["declared_model"] == "haiku" and claimed["agent_id"] == child


def test_the_delegate_call_is_a_feed_row_targeting_the_item(client, key, db):
    """17."""
    planner = _agent(client, key, "planner")
    item = _item(client, key)
    _ok(_delegate(client, key, item, planner))
    db.expire_all()
    rows = db.scalars(select(AgentCall).where(AgentCall.tool == "delegate")).all()
    assert len(rows) == 1
    assert rows[0].target == item and rows[0].agent_id == planner and rows[0].ok is True


def test_routers_do_not_touch_the_delegation_model():
    """18 / A12 extended."""
    for name in ("live.py", "fleet.py"):
        assert "Delegation" not in (ROUTERS / name).read_text(encoding="utf-8"), name


def test_delegate_is_tiered_out_of_a_core_manifest_and_still_dispatches(client, auth, proj):
    """20 / D3."""
    core_key = client.post("/api/api-keys", json={"name": "core-only", "project_id": proj,
                                                  "tool_tiers": []}, headers=auth).json()["plaintext"]
    listed = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"X-API-Key": core_key}).json()["result"]["tools"]
    assert "delegate" not in {t["name"] for t in listed}
    me = _agent(client, core_key, "planner")
    item = _item(client, core_key)
    assert _ok(_delegate(client, core_key, item, me))["state"] == "open"

    fleet_key = client.post("/api/api-keys", json={"name": "fleet", "project_id": proj,
                                                   "tool_tiers": ["fleet"]}, headers=auth).json()["plaintext"]
    listed = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"X-API-Key": fleet_key}).json()["result"]["tools"]
    assert "delegate" in {t["name"] for t in listed}


def test_the_board_query_is_bounded(client, key, proj, db):
    """19 / D19: everything open plus the last ten closed, per delegator."""
    planner = _agent(client, key, "planner")
    for i in range(13):
        item = _item(client, key, f"w{i}")
        d = _ok(_delegate(client, key, item, planner))
        stranger = _agent(client, key, f"s{i}")
        items_svc.claim_item(db, item, stranger)  # each one superseded
    open_item = _item(client, key, "still open")
    _ok(_delegate(client, key, open_item, planner))
    board = dsvc.for_board(db, proj)[planner]
    assert board["closed"] == 13 and board["open"] == 1
    assert len(board["rows"]) == 1 + dsvc.BOARD_CLOSED_MAX
