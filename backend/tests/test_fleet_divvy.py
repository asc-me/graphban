"""D4 — the divvy over MCP (GRPH-335 / PRD-17).

**Accept:** with three workers registered and a backlog of overlapping items, no two
concurrently-held clusters share a touch-area. A fourth worker with no non-colliding cluster
available gets `{claimed: false, held_by: [...], reason: "...held by X, frees in Ns"}`
rather than a colliding one.

The subtlety the whole slice exists for: `collision_clusters` partitions a **snapshot**. As
work lands, actual touchpoints replace predicted ones and the partition moves under the
fleet's feet — so handing clusters out from a stale snapshot re-introduces exactly the
collisions the divvy prevents. An assignment therefore RESERVES its areas, and every later
hand-out is checked against reservations in flight rather than against the partition alone.

Reservations expire lazily at read time rather than being swept. A sweeper would add a failure
mode lazy evaluation cannot have: stop it and the divvy silently freezes, every cluster
looking permanently taken with no error anywhere to explain it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AreaReservation, Item
from app.services import fleet


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
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    """Its own project — the seeded dataset would supply extra ready work and let a
    'collides with everything' assertion pass by finding something unrelated."""
    return client.post("/api/projects", json={"name": "FleetDivvy"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "divvy", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _item(client, key, title, areas):
    return _ok(client, key, "create_item",
               {"title": title, "status": "next", "touchpoints": areas})


def _worker(client, key, label):
    # These share one credential, so each declares a distinct `instance` — on a shared key an
    # agent must show something that differs to count as independent.
    return _ok(client, key, "register_agent",
               {"label": label, "role_hint": "worker", "capabilities": {"instance": label}})


# ---- areas overlap by prefix, not by string equality ---------------------------------------

def test_a_directory_and_a_file_inside_it_are_the_same_touch_area():
    """Compared as distinct strings, `services/` and `services/fleet.py` look non-colliding —
    so both get handed out and two agents edit the same file.

    The PARTITION misses this one: `_match` compares parent directories, which differ here.
    That is why `areas_collide` is the union of both rules rather than either alone."""
    assert fleet.areas_collide(["backend/app/services/fleet.py"], ["backend/app/services"])
    assert fleet.areas_collide(["backend/app/services"], ["backend/app/services/fleet.py"])
    assert not fleet.areas_collide(["backend/app/services"], ["web/src/lib"])


def test_area_comparison_ignores_case_and_trailing_slashes():
    assert fleet.areas_collide(["Backend/App/"], ["backend/app"])


def test_the_reservation_is_never_laxer_than_the_partition():
    """Siblings sharing a parent are one cluster to the partition. If the reservation
    disagreed, it would hand out work the partition had already judged colliding — the exact
    failure reservations exist to prevent."""
    from app.services.clustering import _match

    assert _match("area/0", "area/1"), "the partition relates these"
    assert fleet.areas_collide(["area/0"], ["area/1"]), "so the reservation must too"


# ---- the divvy -------------------------------------------------------------------------------

def test_two_workers_never_hold_overlapping_areas(client, key, db):
    """The easy half of the criterion: work that the STATIC partition already separates.

    Kept, but note what it does not prove — sabotaging the reservation check leaves it green,
    because these two items never collide in the first place. The test below is the one that
    isolates the reservation, and this one would have stood in for it unnoticed.
    """
    _item(client, key, "A", ["backend/app/services/items.py"])
    _item(client, key, "B", ["web/src/features/tracker"])
    w1 = _worker(client, key, "w1")
    w2 = _worker(client, key, "w2")

    first = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"]})
    second = _ok(client, key, "claim_cluster", {"agent_id": w2["agent_id"]})

    assert first["claimed"] and second["claimed"]
    assert not fleet.areas_collide(first["areas"], second["areas"])


def test_a_reservation_holds_when_the_partition_moves_underneath_it(client, key, db):
    """The reason reservations exist at all, and the case a snapshot cannot cover.

    `collision_clusters` partitions the READY pool. Once an item is claimed it leaves that
    pool, so a later partition knows nothing about the areas it is holding. Here item B's
    touchpoints are corrected — the AL-201 capture loop doing its job — to areas worker 1 is
    already editing. A fresh partition puts B in a clean cluster of its own and would hand it
    straight out; only the in-flight reservation knows better.

    Sabotaging the reservation check leaves the test above green and fails this one, which is
    the whole reason it is written separately.
    """
    a = _item(client, key, "A", ["backend/app/services/items.py"])
    b = _item(client, key, "B", ["web/src/features/tracker"])
    w1 = _worker(client, key, "w1")
    w2 = _worker(client, key, "w2")
    got = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"], "max_items": 1})
    assert got["claimed"] and got["items"][0]["id"] == a["id"]

    # The partition moves: B turns out to touch what w1 is already editing.
    _ok(client, key, "update_item",
        {"id": b["id"], "touchpoints": ["backend/app/services/items.py"]})
    fresh = _ok(client, key, "collision_clusters", {})
    assert any(b["id"] in c["items"] for c in fresh["clusters"]), \
        "the static partition still offers B — that is the premise"

    out = _ok(client, key, "claim_cluster", {"agent_id": w2["agent_id"]})

    assert out["claimed"] is False, "the reservation, not the partition, is what refused it"
    # Names the holder rather than describing the situation: "collides with in-flight work" is
    # true of an abandoned lease too, and the caller cannot tell which without the agent id.
    assert out["held_by"] == [w1["agent_id"]]
    assert w1["agent_id"] in out["reason"]


def test_a_worker_with_nothing_non_colliding_is_told_so(client, key, db):
    """The fourth worker. `claimed: false` with a reason is a real answer — handing it a
    colliding cluster to avoid an empty response is the failure this slice prevents."""
    _item(client, key, "A", ["backend/app/services/items.py"])
    _item(client, key, "B", ["backend/app/services/items.py"])
    w1 = _worker(client, key, "w1")
    w2 = _worker(client, key, "w2")
    got = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"], "max_items": 1})
    assert got["claimed"]

    out = _ok(client, key, "claim_cluster", {"agent_id": w2["agent_id"]})

    assert out["claimed"] is False
    assert out["held_by"] == [w1["agent_id"]]
    assert "frees in" in out["reason"], "how long to wait is the actionable half"


def test_a_claim_reserves_its_areas_in_the_same_breath(client, key, db):
    """Written in the same transaction as the claims that justify them. A follow-up write
    leaves a window in which items are claimed but their areas unreserved — and a second agent
    claims straight through it."""
    _item(client, key, "A", ["backend/app/services/items.py"])
    w1 = _worker(client, key, "w1")

    _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"]})

    rows = db.query(AreaReservation).filter(AreaReservation.agent_id == w1["agent_id"]).all()
    assert rows, "the claim left no reservation behind"
    assert any("services/items.py" in r.area for r in rows)


def test_an_agents_own_reservation_does_not_block_it(client, key, db):
    """A worker asking for more work must not be refused because of the cluster it is already
    holding — that would make a second `claim_cluster` impossible for the agent doing the
    work."""
    _item(client, key, "A", ["backend/app/services/items.py"])
    _item(client, key, "B", ["web/src/features/tracker"])
    w1 = _worker(client, key, "w1")

    first = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"], "max_items": 1})
    second = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"], "max_items": 1})

    assert first["claimed"] and second["claimed"]


# ---- reservation lifecycle ------------------------------------------------------------------

def test_an_expired_reservation_stops_blocking_without_a_sweeper(client, key, db):
    """Lazy at read time. A sweeper would add a failure mode this cannot have: stop it and the
    divvy freezes with every cluster looking permanently taken and nothing to explain it."""
    _item(client, key, "A", ["backend/app/services/items.py"])
    w1 = _worker(client, key, "w1")
    _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"]})
    for row in db.query(AreaReservation).all():
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert fleet.active_reservations(db) == []


def test_releasing_an_item_frees_its_areas(client, key, db):
    """They expire anyway — but an area held for the rest of a lease that nobody is editing is
    a cluster the divvy will not hand out, so the fleet idles for ten minutes on work that
    already stopped."""
    _item(client, key, "A", ["backend/app/services/items.py"])
    w1 = _worker(client, key, "w1")
    got = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"]})
    assert db.query(AreaReservation).count() > 0

    _ok(client, key, "release_item",
        {"id": got["items"][0]["id"], "agent_id": w1["agent_id"]})

    assert db.query(AreaReservation).count() == 0


def test_signing_off_frees_the_areas(client, key, db):
    _item(client, key, "A", ["backend/app/services/items.py"])
    w1 = _worker(client, key, "w1")
    got = _ok(client, key, "claim_cluster", {"agent_id": w1["agent_id"]})
    _ok(client, key, "update_item",
        {"id": got["items"][0]["id"], "status": "review", "agent_id": w1["agent_id"]})
    rev = _ok(client, key, "register_agent",
               {"label": "r", "role_hint": "reviewer", "capabilities": {"instance": "r"}})

    _ok(client, key, "sign_off", {"id": got["items"][0]["id"], "agent_id": rev["agent_id"]})

    assert db.query(AreaReservation).count() == 0


# ---- the capture loop -------------------------------------------------------------------------

def test_actual_touchpoints_replace_the_prediction(client, key, db):
    """A prediction never corrected keeps mis-partitioning the same files forever, while
    `collision_clusters` reports clean separation and the fleet keeps colliding on them.

    No new tool for this: `update_item(touchpoints=…)` already replaces them, and a second
    write path would be a fork of the same field.
    """
    created = _item(client, key, "A", [])
    before = _ok(client, key, "collision_clusters", {})
    assert any(c["predicted"] for c in before["clusters"]), "no prediction to correct"

    _ok(client, key, "update_item",
        {"id": created["id"], "touchpoints": ["backend/app/services/items.py"]})

    after = _ok(client, key, "collision_clusters", {})
    mine = [c for c in after["clusters"] if created["id"] in c["items"]]
    assert mine and mine[0]["predicted"] is False
    assert "backend/app/services/items.py" in mine[0]["areas"]


def test_clusters_are_returned_as_rendered_keys(client, key):
    """An agent quotes these back, and `services/keys` resolves the rendered form. Emitting a
    stored id would hand it a string that survives a retag as a dangling reference."""
    created = _item(client, key, "A", ["backend/app/services/items.py"])

    out = _ok(client, key, "collision_clusters", {})

    ids = [i for c in out["clusters"] for i in c["items"]]
    assert created["id"] in ids


def test_a_planner_may_read_the_partition_but_not_claim_it(client, key):
    """The orchestrator allocates; it does not quietly take the work."""
    planner = _ok(client, key, "register_agent", {"label": "p", "role_hint": "planner"})
    _item(client, key, "A", ["backend/app/services/items.py"])

    assert _ok(client, key, "collision_clusters", {"agent_id": planner["agent_id"]})["total"] >= 1
    res = _rpc(client, key, "claim_cluster", {"agent_id": planner["agent_id"]})
    assert res.get("isError") is True
    assert res["structuredContent"]["error"]["code"] == "unauthorized"
