"""A dead agent stops blocking the divvy (GRPH-808).

Reported from super-arc: holdings are already flagged `phase: "stale"`, `phase_basis: "agent
offline"`. The server knew the holder was dead, knew the reservation blocked others, and did
nothing with it — while a working release path existed.

The cost is arithmetic. Presence lapses at `lease_seconds // 4` (150s on the default) and a
reservation runs to `lease_seconds` (600s), so a provably dead agent kept everyone out of its
files for the remaining 450s. A wave of dead children stacks them.

`offline` is already conservative and was built to be: an agent misses THREE consecutive
heartbeats before it counts as gone, so "one slow network round trip never releases a working
agent's items". This spends that existing margin rather than inventing a shorter one.
"""
from datetime import timedelta

import pytest

from app.services import fleet as fleet_svc
from app.services import items as items_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, touchpoints):
    r = client.post("/api/items", json={"title": title, "project_id": "core",
                                        "touchpoints": touchpoints}, headers=auth)
    return r.json()["id"]


def _agent(db, agent_id: str, *, seen_seconds_ago: float):
    from app.models import Agent

    now = items_svc.utcnow()
    row = db.get(Agent, agent_id)
    if row is None:
        row = Agent(id=agent_id, project_id="core", number=abs(hash(agent_id)) % 9000,
                    label=agent_id, active_role="worker")
        db.add(row)
    row.last_seen_at = now - timedelta(seconds=seen_seconds_ago)
    db.commit()
    return row


def _reserve(db, agent_id, item_id, areas, *, seconds=600):
    fleet_svc.reserve_areas(db, agent_id=agent_id, item_id=item_id, areas=areas,
                            expires_at=items_svc.utcnow() + timedelta(seconds=seconds),
                            predicted=False)
    db.commit()


def _held(db) -> set[str]:
    return {r.area for r in fleet_svc.active_reservations(db, "core")}


# ---- the three answers ------------------------------------------------------------------------

def test_a_live_holder_still_holds(client, auth, db):
    """The control, and the direction that would actually hurt. Releasing a working agent's
    areas puts two agents in one file."""
    item = _item(client, auth, "live work", ["live/a.py"])
    _agent(db, "CORE-A1", seen_seconds_ago=5)
    _reserve(db, "CORE-A1", item, ["live/a.py"])

    assert "live/a.py" in _held(db)


def test_an_offline_holder_holds_nothing(client, auth, db):
    """THE FIX. 450 seconds of blocking, per dead agent, on a signal the server already had."""
    item = _item(client, auth, "dead work", ["dead/a.py"])
    _agent(db, "CORE-A2", seen_seconds_ago=fleet_svc.presence_ttl_seconds() + 30)
    _reserve(db, "CORE-A2", item, ["dead/a.py"])

    assert "dead/a.py" not in _held(db)


def test_an_agent_that_cannot_be_found_is_not_called_offline(client, auth, db):
    """"I could not find the holder" is not "the holder is gone". Releasing on ignorance is
    how two agents end up editing one file.

    Asserted on the function rather than through the database, because `area_reservations`
    has a foreign key to `agents` — an orphaned reservation cannot exist, and the first
    version of this test failed trying to manufacture one. The branch stays because a
    predicate that quietly answered "offline" for anything it could not resolve would be a
    different function from the one this file is about.
    """
    assert fleet_svc._offline_holders(db, {"NOBODY-A1"}, now=items_svc.utcnow()) == set()
    assert fleet_svc._offline_holders(db, set(), now=items_svc.utcnow()) == set()


# ---- the margin it spends is the one already there ------------------------------------------------

def test_a_holder_inside_the_presence_window_is_not_released(client, auth, db):
    """An agent that missed one beat is not gone. The 3x gap exists so a slow round trip
    never costs a working agent its work, and this must not shorten it."""
    item = _item(client, auth, "slow work", ["slow/a.py"])
    _agent(db, "CORE-A4", seen_seconds_ago=fleet_svc.presence_ttl_seconds() - 5)
    _reserve(db, "CORE-A4", item, ["slow/a.py"])

    assert "slow/a.py" in _held(db)


def test_expiry_still_applies_to_a_live_holder(client, auth, db):
    """The old rule is untouched: a reservation past its horizon is gone whoever holds it."""
    item = _item(client, auth, "expired work", ["old/a.py"])
    _agent(db, "CORE-A5", seen_seconds_ago=1)
    _reserve(db, "CORE-A5", item, ["old/a.py"], seconds=-30)

    assert "old/a.py" not in _held(db)


def test_one_dead_agent_does_not_free_a_live_ones_areas(client, auth, db):
    """Two holders, one dead. The filter is per-reservation, not per-project."""
    a = _item(client, auth, "dead", ["mixed/dead.py"])
    b = _item(client, auth, "live", ["mixed/live.py"])
    _agent(db, "CORE-A6", seen_seconds_ago=fleet_svc.presence_ttl_seconds() + 60)
    _agent(db, "CORE-A7", seen_seconds_ago=2)
    _reserve(db, "CORE-A6", a, ["mixed/dead.py"])
    _reserve(db, "CORE-A7", b, ["mixed/live.py"])

    held = _held(db)
    assert "mixed/dead.py" not in held
    assert "mixed/live.py" in held
