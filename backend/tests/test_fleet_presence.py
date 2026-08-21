"""Presence on the graph: which node is held, by whom, and what could not be placed (D4)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AreaReservation
from app.services import code_graph as cg
from app.services import fleet as fleet_svc


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
