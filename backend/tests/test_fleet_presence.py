"""Presence on the graph: which node is held, by whom, and what could not be placed (D4)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AreaReservation
from app.services import code_graph as cg
from app.services import fleet as fleet_svc

from tests.decoy import assert_populated


@pytest.fixture
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── area_matches: the presence matcher, deliberately not clustering._match ────

def test_area_matches_exact_and_glob_and_directory_prefix():
    assert cg.area_matches("a/b.py", "a/b.py")
    assert cg.area_matches("a/*", "a/b.py")
    assert cg.area_matches("a", "a/b.py")
    assert cg.area_matches("a/", "a/b.py")


def test_area_matches_does_not_light_directory_siblings():
    """The whole reason this is not `clustering._match`.

    `_match`'s third rule calls any two paths sharing a parent directory related, which is SAFE
    for the collision divvy — over-block, never collide — and a LIE for presence, because it
    claims an agent is somewhere it is not. Measured against the live graph it turned one file
    area into 25 nodes.
    """
    assert not cg.area_matches("a/b.py", "a/c.py")
    assert not cg.area_matches("backend/app/services/items.py", "backend/app/services/fleet.py")


def test_area_matches_covers_symbols_of_a_covered_file():
    assert cg.area_matches("a/b.py", "a/b.py::claim_next")
    assert cg.area_matches("a", "a/b.py::claim_next")
    assert cg.area_matches("a/*", "a/b.py::claim_next")


def test_a_symbol_area_does_not_cover_its_siblings():
    # `a.py::x` must not quietly cover `a.py::y` — the sibling bug, one level down.
    assert cg.area_matches("a/b.py::x", "a/b.py::x")
    assert not cg.area_matches("a/b.py::x", "a/b.py::y")
    # ...and must not widen to the whole file either.
    assert not cg.area_matches("a/b.py::x", "a/b.py")


def test_a_glob_over_symbols_still_matches():
    """Pins the symbol handling from the other side.

    The file-stripping recursion must not swallow an area that is ITSELF symbol-qualified: a
    `*::claim_next` area has to keep matching `items.py::claim_next`, or symbol-level
    reservations would silently resolve to nothing the day GRPH-382 makes symbols exist.
    """
    assert cg.area_matches("*::claim_next", "backend/app/services/items.py::claim_next")
    assert not cg.area_matches("*::claim_next", "backend/app/services/items.py::release")
    assert cg.area_matches("*", "backend/app/services/items.py::claim_next")


def test_area_matches_is_not_a_bare_prefix_match():
    # `a/b` must not cover `a/bc.py` — a directory prefix needs the separator.
    assert not cg.area_matches("a/b", "a/bc.py")


def test_area_matches_handles_empty_input():
    assert not cg.area_matches("", "a.py")
    assert not cg.area_matches("a.py", "")


# ── held_areas ────────────────────────────────────────────────────────────────

def _item_id(db, project_id="core"):
    """A real item in the project. `active_reservations` scopes by item, so a reservation
    pointing at an id that is not in the project is correctly invisible."""
    from app.models import Item
    from sqlalchemy import select

    return db.scalars(select(Item).where(Item.project_id == project_id)).first().id


def _agent(db, label="a1", project_id="core"):
    """A REAL agent row, registered the way the product registers one.

    An earlier version of this fixture invented `agent_id="GRPH-A1"` and passed on SQLite,
    which does not enforce foreign keys by default — Postgres does, and CI runs both. The
    reservation is also what the holder join reads, so a made-up id was testing the join
    against nothing: no `ApiKey`, therefore no `User`, therefore no colour.
    """
    from sqlalchemy import select

    from app.models import ApiKey
    from app.services import fleet as fs

    key = db.scalars(select(ApiKey).where(ApiKey.project_id == project_id)).first()
    if key is None:  # pragma: no cover — the seeded dataset always has one
        key = db.scalars(select(ApiKey)).first()
    agent = fs.register_agent(db, project_id=project_id, api_key=key, label=label)
    db.commit()
    return agent.id


def _reserve(db, agent_id, area, item_id=None, seconds=600, predicted=False):
    db.add(AreaReservation(
        agent_id=agent_id, item_id=item_id or _item_id(db), area=area,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        predicted=predicted,
    ))
    db.commit()


def test_held_resolves_an_area_to_exactly_its_node(db):
    for p in ("backend/app/services/items.py", "backend/app/services/fleet.py"):
        cg.upsert_node(db, project_id="core", path=p, kind="module", name=p)
    db.commit()
    _reserve(db, _agent(db), "backend/app/services/items.py")

    out = fleet_svc.held_areas(db, "core")
    assert len(out["held"]) == 1
    # One node, not the directory. `_match` would have returned both.
    assert out["held"][0]["node_paths"] == ["backend/app/services/items.py"]
    assert out["off_map"] == []


def test_an_unplaceable_area_is_reported_not_dropped(db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    _reserve(db, _agent(db), "vercel env")

    out = fleet_svc.held_areas(db, "core")
    assert out["held"] == []
    assert len(out["off_map"]) == 1
    assert out["off_map"][0]["area"] == "vercel env"
    assert out["off_map"][0]["reason"] == "undescribed"


def test_a_stale_node_is_reported_rather_than_glowing_as_current(db):
    cg.upsert_node(db, project_id="core", path="gone.py", kind="module", name="g")
    db.commit()  # mark_paths_stale SELECTs, so the node has to be visible first
    assert cg.mark_paths_stale(db, "core", ["gone.py"]) == 1
    db.commit()
    _reserve(db, _agent(db), "gone.py")

    out = fleet_svc.held_areas(db, "core")
    assert out["held"] == []
    assert out["off_map"][0]["reason"] == "stale"


def test_the_payload_carries_the_holder_and_the_lease_clock(db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    _reserve(db, _agent(db), "a.py")

    out = fleet_svc.held_areas(db, "core")
    row = out["held"][0]
    for field in ("agent_id", "agent_label", "active_role", "state",
                  "user_id", "user_initials", "user_color", "expires_at", "predicted"):
        assert field in row, field
    # Echoed so the client needs no second call to learn its poll cadence.
    assert out["heartbeat_interval_seconds"] == fleet_svc.heartbeat_interval_seconds()
    assert out["served_at"]


def test_an_expired_reservation_stops_holding_with_no_sweeper(db):
    """AC-10: the glow fades by construction, on the lease clock, with nothing swept."""
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    _reserve(db, _agent(db), "a.py", seconds=600)

    assert len(fleet_svc.held_areas(db, "core")["held"]) == 1
    later = datetime.now(timezone.utc) + timedelta(seconds=601)
    after = fleet_svc.held_areas(db, "core", now=later)
    assert after["held"] == [] and after["off_map"] == [] and after["total"] == 0


def test_the_cap_truncates_visibly_and_reports_the_true_total(db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    for i in range(5):
        _reserve(db, _agent(db, label=f"a{i}"), "a.py")

    out = fleet_svc.held_areas(db, "core", cap=2)
    assert out["truncated"] is True
    # The true total, not the truncated length — a silent cut reads as a complete answer.
    assert out["total"] == 5
    assert len(out["held"]) == 2


def test_an_untruncated_payload_says_so(db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    _reserve(db, _agent(db), "a.py")
    out = fleet_svc.held_areas(db, "core")
    assert out["truncated"] is False and out["total"] == 1


def test_the_truncated_prefix_is_deterministic(db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    for i in range(5):
        _reserve(db, _agent(db, label=f"a{i}"), "a.py")
    first = fleet_svc.held_areas(db, "core", cap=2)["held"]
    second = fleet_svc.held_areas(db, "core", cap=2)["held"]
    assert [r["agent_id"] for r in first] == [r["agent_id"] for r in second]


def test_an_empty_fleet_is_shaped_not_absent(db):
    out = fleet_svc.held_areas(db, "core")
    assert out["held"] == [] and out["off_map"] == []
    assert out["total"] == 0 and out["truncated"] is False
    assert out["heartbeat_interval_seconds"] > 0


# ── the route: JWT only ───────────────────────────────────────────────────────

def test_presence_route_serves_a_member(client, auth, db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    db.commit()
    _reserve(db, _agent(db), "a.py")
    r = client.get("/api/fleet/presence?project_id=core", headers=auth)
    assert r.status_code == 200
    assert len(r.json()["held"]) == 1


def test_presence_route_refuses_an_anonymous_caller(client):
    assert client.get("/api/fleet/presence?project_id=core").status_code == 401


def test_presence_is_not_reachable_with_an_agent_api_key(client, auth):
    """Privacy, not politeness: this payload names which HUMAN is editing which file.

    An agent has no use for it — `graph_query` answers what depends on the code it is about to
    touch — and shipping it on the MCP surface would put a live map of everyone's activity
    behind every credential in the fleet.
    """
    key = client.post(
        "/api/api-keys", json={"name": "spy", "project_id": "core"}, headers=auth
    ).json()["plaintext"]
    r = client.get("/api/fleet/presence?project_id=core", headers={"X-API-Key": key})
    assert r.status_code == 401

    from app.mcp_server import TOOLS
    assert "fleet_presence" not in {t["name"] for t in TOOLS}
    assert "held_areas" not in {t["name"] for t in TOOLS}


# ---- the dashed channel (GRPH-387) ----------------------------------------------


def test_a_predicted_hold_says_so_rather_than_reading_as_declared(db):
    """`held_areas` returned `predicted: False` for every row, so the graph's dashed
    "guess" channel could never light from the API.

    The distinction is the point of the channel. An item with touchpoints a human wrote is
    a claim; an item whose areas were inferred from the code map is a guess the fleet will
    still honour — and drawing the second like the first asserts a confidence nobody has.

    `test_the_payload_carries_the_holder_and_the_lease_clock` only asserted the KEY was
    present, which a hardcoded literal satisfies. This asserts the value.
    """
    cg.upsert_node(db, project_id="core", path="a.py", kind="module", name="a")
    cg.upsert_node(db, project_id="core", path="b.py", kind="module", name="b")
    db.commit()
    agent = _agent(db)
    _reserve(db, agent, "a.py", predicted=True)
    _reserve(db, agent, "b.py", predicted=False)

    rows = {r["area"]: r for r in fleet_svc.held_areas(db, "core")["held"]}
    assert rows["a.py"]["predicted"] is True
    assert rows["b.py"]["predicted"] is False


def test_presence_is_pinned_to_its_project(db, decoy):
    """Removing the project scope must fail HERE (GRPH-436).

    Swapping `active_reservations(db, project_id, now=now)` for `active_reservations(db,
    None, now=now)` inside `held_areas` left all twenty tests in this file green, because
    every one of them seeds `core` alone and with one project the two calls return the same
    rows. Measured during the review of this slice, not supposed.

    The decoy holds real areas through a real agent on a real key — the same insistence
    `_agent` already documents — so what is excluded here is a row that genuinely exists.
    """
    assert_populated(db, decoy)
    mine = _agent(db, "core-agent")
    _reserve(db, mine, "backend/app/services/items.py")

    out = fleet_svc.held_areas(db, "core")

    seen = {e["area"] for e in out["held"] + out["off_map"]}
    holders = {e["agent_id"] for e in out["held"] + out["off_map"]}
    assert "backend/app/services/items.py" in seen, "core cannot see its own reservation"
    assert not (seen & set(decoy["areas"])), f"presence leaked another project's areas: {seen}"
    assert decoy["agent_id"] not in holders, "presence leaked another project's agent"
    assert out["total"] == 1, f"total counts rows outside the project: {out['total']}"


# ── GRPH-1069: a long tool call keeps the claim; a dead child does not ────────

#: The report's tool call. Longer than the 150s presence TTL, shorter than the lease.
_TOOL_CALL_SECONDS = 300


def _open_attempt(db, label: str):
    """An in-progress item, its worker, and a linked attempt that has not ended."""
    from app.models import Agent, Delegation
    from app.services import items as items_svc

    worker = _agent(db, label=f"{label}-child")
    delegator = _agent(db, label=f"{label}-planner")
    item = items_svc.create_item(
        db, title=label, project_id="core", status="in_progress", kind="chore")
    now = datetime.now(timezone.utc)
    item.claimed_by = worker
    item.built_by = worker
    item.claimed_at = now
    item.assignee = worker
    item.branch = "gb/obs"
    agent = db.get(Agent, worker)
    assert agent is not None
    agent.last_seen_at = now
    agent.state = "working"
    attempt = Delegation(
        id=f"d-{item.id}", project_id="core", item_id=item.id,
        delegated_by=delegator, agent_id=worker, linked_by="seat",
        lane="backend", requested_tier="cheap", claimed_at=now, lease_seconds=600,
    )
    db.add(attempt)
    db.commit()
    db.refresh(item)
    return item, agent, attempt


def test_a_five_minute_tool_call_keeps_the_claim_and_writes_no_released(db):
    """Six beats, each arriving after presence has already lapsed, cover more than
    five minutes. The supervisor posts the lease before `fleet_status` requeues, so
    the item stays `in_progress` and the attempt stays open.

    Sabotage: delete `agent.last_seen_at = now` in `_record_lease`. The first requeue
    after the aged stamp releases the item, and `item.id not in released` fails.
    """
    item, agent, attempt = _open_attempt(db, "five-minute-call")
    ttl = fleet_svc.presence_ttl_seconds()
    beats = 6
    assert beats * (ttl + 1) >= _TOOL_CALL_SECONDS
    who = agent.id

    for _ in range(beats):
        agent.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=ttl + 1)
        db.flush()
        reply = fleet_svc._record_lease(
            db, item, branch="gb/obs", agent_id=who, commit=None)
        assert reply["extended"] is True
        db.flush()
        released = fleet_svc.requeue_offline_items(db, project_id="core")
        assert item.id not in released
        db.refresh(item)
        assert item.status == "in_progress"
        assert item.claimed_by == who
        db.refresh(attempt)
        assert attempt.outcome is None


def test_a_dead_child_requeues_within_one_presence_ttl(db):
    """Posts have stopped. Exactly one TTL is still present (`>` , not `>=`). One
    second inside that bound, `requeue_offline_items` leaves the item; two seconds
    past it, the item is `next` and the attempt is `released`. `claimed_at` is still
    well inside the item lease, so this is the presence clock.

    Sabotage: stop the proxy posts — do not call `_record_lease` again after the
    stamp ages past the TTL. This test is that stop. Raising the TTL would keep the
    aged stamp inside the window and the release assertion would fail.
    """
    from app.services.items import DEFAULT_LEASE_SECONDS

    item, agent, attempt = _open_attempt(db, "dead-child")
    who = agent.id
    ttl = fleet_svc.presence_ttl_seconds()
    reply = fleet_svc._record_lease(
        db, item, branch="gb/obs", agent_id=who, commit=None)
    assert reply["extended"] is True
    db.flush()
    db.refresh(item)

    seen = datetime.now(timezone.utc) - timedelta(seconds=ttl)
    agent.last_seen_at = seen
    # Equal to the TTL is not offline. A microsecond past it is.
    assert fleet_svc.presence_state(agent, now=seen + timedelta(seconds=ttl)) != "offline"
    assert fleet_svc.presence_state(
        agent, now=seen + timedelta(seconds=ttl, microseconds=1)) == "offline"

    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=ttl - 1)
    db.flush()
    db.refresh(item)
    lease_age = (datetime.now(timezone.utc) - fleet_svc._aware(item.claimed_at)).total_seconds()
    assert lease_age < DEFAULT_LEASE_SECONDS
    held = fleet_svc.requeue_offline_items(db, project_id="core")
    assert item.id not in held
    db.refresh(item)
    db.refresh(attempt)
    assert item.status == "in_progress" and item.claimed_by == who
    assert attempt.outcome is None

    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=ttl + 2)
    db.flush()
    db.refresh(item)
    lease_age = (datetime.now(timezone.utc) - fleet_svc._aware(item.claimed_at)).total_seconds()
    assert lease_age < 30
    assert lease_age < DEFAULT_LEASE_SECONDS
    released = fleet_svc.requeue_offline_items(db, project_id="core")
    assert item.id in released
    db.refresh(item)
    db.refresh(attempt)
    assert item.status == "next"
    assert item.claimed_by is None
    assert attempt.outcome == "released"
