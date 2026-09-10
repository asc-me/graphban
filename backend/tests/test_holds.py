"""What is held, by whom, and until when (GRPH-833).

Reported as O5. The cost of not having this surface was a WRONG conclusion, not a slow one:
mid-wave, with full repository access, the item touchpoints in hand and time to think, the
operator decided an item was being wrongly held and inferred directory-level clustering from
the symptom. A later spawn into a genuinely disjoint cluster disproved it. `--max-workers 3`
yielding one running child is a symptom anyone can see; why, was readable nowhere.

Clusters have carried `held_by` and `free_in` since GRPH-803 and it was not enough, for a
reason worth stating because it is the whole design here: **a cluster is only in the partition
while its items are claimable.** The moment an item is claimed its cluster leaves the pool,
taking its reservation off every read — while that reservation goes on blocking everybody
else. So the wave with nothing to spawn had nothing to show for itself either.

Two answers, deliberately separate:

- `holds` is keyed on the RESERVATION, so a hold is listed whether or not the work it belongs
  to is still on offer;
- `held_because` is keyed on the CLUSTER, and says which of its areas the hold covers and by
  which rule — the half the operator was left to infer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import select

from app.models import Agent, AreaReservation
from app.services import collision as collision_svc
from app.services import fleet as fleet_svc


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
    return client.post("/api/projects", json={"name": "Holds"}, headers=auth).json()["id"]


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


def _item(client, key, title, touchpoints) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": touchpoints}))["id"]


def _agent(client, key, label="worker") -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label}))["agent_id"]


def _reserve(db, *, agent_id, item_id, areas, seconds=600, predicted=False):
    """A real reservation through the real writer, so this cannot drift from the claim path."""
    from app.services import keys

    stored = keys.resolve_item(db, item_id) or item_id
    fleet_svc.reserve_areas(
        db, agent_id=agent_id, item_id=stored, areas=areas,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        predicted=predicted)
    db.commit()
    return stored


def _go_offline(db, agent_id, minutes=90):
    row = db.get(Agent, agent_id)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.commit()


# ---- the table ---------------------------------------------------------------------------

def test_a_hold_is_listed_even_when_its_item_left_the_partition(client, key, db, proj):
    """THE ONE THAT MATTERS. The item is claimed, so its cluster is not in the divvy at all —
    and its reservation is still blocking everybody. That is precisely the state the operator
    was staring at with no read that could show it.

    Sabotage: build `holds` from `clusters_for_project` instead of from the reservations and
    this returns nothing at all."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["platform-models/route.ts"])
    stored = _reserve(db, agent_id=holder, item_id=item, areas=["platform-models"])
    from app.services import items as items_svc

    items_svc.claim_item(db, stored, holder)
    db.commit()

    rows = collision_svc.holds(db, proj)

    assert [r["area"] for r in rows] == ["platform-models"]
    assert rows[0]["agent_id"] == holder
    assert rows[0]["item"] == item


def test_an_ignored_hold_is_listed_and_says_it_is_ignored(client, key, db, proj):
    """THE ONE THE SUITE CORRECTED ME ON. `active_reservations` already drops an offline
    holder's rows so they stop blocking (GRPH-808) — so a diagnostic built on it could only
    ever report live holders, and `holder_state` would have carried a branch that can never
    fire: a field that reads as protection and is not.

    The row still EXISTS, and an operator who can see it but not that it is ignored goes
    looking for a collision that is not happening. Listing it and saying so is the answer.

    Sabotage: build `holds` on `active_reservations` and this returns nothing, while every
    other test in this file stays green."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["a/b.py"])
    _reserve(db, agent_id=holder, item_id=item, areas=["a"])
    _go_offline(db, holder)

    rows = collision_svc.holds(db, proj)

    assert rows and rows[0]["holder_state"] in ("offline", "retired")
    assert rows[0]["blocking"] is False, "listed a dead hold as if it were keeping people out"
    assert isinstance(rows[0]["free_in"], int)


def test_offline_and_retired_are_different_answers(client, key, db, proj):
    """They look identical from a cluster and call for different moves: an offline holder's
    lease will lapse, and a retired seat can never register again — so nothing is coming back
    to release it early. Two states, because "wait" and "stop waiting" are two instructions."""
    from app.services import fleet as fleet_svc

    short = _agent(client, key, "briefly gone")
    item_a = _item(client, key, "a", ["a/x.py"])
    _reserve(db, agent_id=short, item_id=item_a, areas=["a"])
    _go_offline(db, short, minutes=5)

    rows = {r["agent_id"]: r for r in collision_svc.holds(db, proj)}

    assert rows[short]["holder_state"] == "offline"
    assert not fleet_svc.retired(db.get(Agent, short)), "the fixture made it retired, not offline"


def test_a_live_holder_reads_as_live(client, key, db, proj):
    """The control. A surface that reported everything as dead would be worse than none — the
    operator would start releasing holds that belong to working agents."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["a/b.py"])
    _reserve(db, agent_id=holder, item_id=item, areas=["a"])

    row = collision_svc.holds(db, proj)[0]

    assert row["holder_state"] not in ("offline", "retired", "unknown")
    assert row["blocking"] is True


def test_a_predicted_area_says_so(client, key, db, proj):
    """A predicted area is a guess about files nobody listed. An operator judging whether a
    hold is legitimate needs to know which kind it is looking at."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["a/b.py"])
    _reserve(db, agent_id=holder, item_id=item, areas=["a"], predicted=True)

    assert collision_svc.holds(db, proj)[0]["predicted"] is True


def test_an_expired_hold_is_not_listed(client, key, db, proj):
    """A hold that has lapsed blocks nothing, and listing it would send the operator chasing a
    reservation that is already gone."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["a/b.py"])
    _reserve(db, agent_id=holder, item_id=item, areas=["a"], seconds=-30)

    assert collision_svc.holds(db, proj) == []


# ---- why THIS cluster is held ------------------------------------------------------------

def test_a_held_cluster_names_the_area_and_the_rule(client, key, db, proj):
    """The inference the operator got wrong, made readable. `because` (GRPH-810) says why a
    cluster's own members merged; this says why the whole cluster is unavailable."""
    from app.services import items as items_svc

    holder = _agent(client, key, "builder")
    held = _item(client, key, "theirs", ["platform-models/list.ts"])
    stored = _reserve(db, agent_id=holder, item_id=held,
                      areas=["platform-models/list.ts"])
    # Claimed, so their item leaves the partition and mine forms its own cluster. What is left
    # relating the two is the DIRECTORY rule — the broad one, and the one the operator
    # inferred and then talked themselves out of.
    items_svc.claim_item(db, stored, holder)
    db.commit()
    mine = _item(client, key, "mine", ["platform-models/route.ts"])

    clusters = collision_svc.clusters_for_project(db, proj)
    got = next(c for c in clusters if mine in c["items"])

    assert got["held_by"] == [holder]
    assert got["held_because"], "held a cluster and gave no reason"
    first = got["held_because"][0]
    assert first["by"] == holder
    assert first["rule"] == "directory", "named no rule; the operator is back to inferring one"


def test_a_free_cluster_carries_no_reason(client, key, db, proj):
    """The control. `held_because` on an unheld cluster would make every reply read as
    contention."""
    _item(client, key, "mine", ["svc/thing.py"])

    got = collision_svc.clusters_for_project(db, proj)[0]

    assert "held_because" not in got


# ---- the tool ------------------------------------------------------------------------------

def test_the_reservation_table_is_returned_only_when_asked(client, key, db, proj):
    """Every wave polls this tool once a second. A table on every reply is paid for by every
    caller to answer a question almost none of them are asking."""
    holder = _agent(client, key, "builder")
    item = _item(client, key, "held work", ["a/b.py"])
    _reserve(db, agent_id=holder, item_id=item, areas=["a"])

    quiet = _ok(_mcp(client, key, "collision_clusters", {}))
    asked = _ok(_mcp(client, key, "collision_clusters", {"holds": True}))

    assert "holds" not in quiet
    assert [r["area"] for r in asked["holds"]] == ["a"]
