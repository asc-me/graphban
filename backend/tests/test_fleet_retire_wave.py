"""PRD-22 §6 — mint, list and retire are one capability, scoped by `minted_by`.

Spin-up was agent-callable and spin-down was not. A planner could mint a seat for an
agent it was about to spawn and then neither retire it nor see what became of it: there
was no `minted_by` scope on `list_enrolments`, seat state was exposed only over REST
behind user auth, and `fleet_status` carried per-agent `enrolled` as a bare boolean —
the consequences of revocation, never the transition.

That was coherent while a human opened every terminal. It stops being coherent the
moment a planner provisions its own fleet, and it fails in the direction that costs
money: a fleet that can grow and not shrink.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent, ApiKey, AreaReservation, Enrolment, Item
from app.services import fleet

from tests.test_fleet_enrolment import _seat, db, key, proj  # noqa: F401


def _plan(db, proj, minter="GRPH-A1", role="worker", wave=None):
    return fleet.issue_enrolment(db, project_id=proj, role=role, wave=wave, minted_by=minter)[0]


def _agent_on(db, proj, seat, *, key_id=None, seen=None):
    agent = Agent(
        id=f"A-{seat.id[:8]}", number=1, project_id=proj, api_key_id=key_id,
        label="w", capabilities={}, enrolment_id=seat.id,
        active_role="worker", state="working",
        registered_at=datetime.now(timezone.utc),
        last_seen_at=seen or datetime.now(timezone.utc),
    )
    db.add(agent)
    db.flush()
    return agent


# --- scope -------------------------------------------------------------------------


def test_it_revokes_only_the_seats_this_planner_minted(db, proj):
    """The bound the whole capability rests on. A planner that could reach another
    planner's seats would be retiring a fleet it never provisioned."""
    mine = _plan(db, proj, minter="GRPH-A1")
    theirs = _plan(db, proj, minter="GRPH-A2")
    hand_minted = fleet.issue_enrolment(db, project_id=proj, role="worker")[0]
    db.flush()

    out = fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    assert out["seats_revoked"] == 1
    assert db.get(Enrolment, mine.id).revoked is True
    assert db.get(Enrolment, theirs.id).revoked is False, "reached another planner's seat"
    assert db.get(Enrolment, hand_minted.id).revoked is False, (
        "revoked a seat nobody minted through a planner — that is end_wave's job, "
        "and only a human presses it"
    )


def test_it_does_not_touch_api_keys(db, proj, client, auth):
    """`end_wave` revokes keys because a human is ending a whole wave. A planner never
    minted a key, and retiring somebody's long-lived credential is a surprise this
    capability never promised."""
    raw = client.post("/api/api-keys", json={"name": "shared", "project_id": proj},
                      headers=auth).json()
    seat = _plan(db, proj, minter="GRPH-A1")
    db.flush()

    fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    assert db.get(ApiKey, raw["id"]).revoked is False


def test_a_wave_filter_narrows_it_further(db, proj):
    early = _plan(db, proj, minter="GRPH-A1", wave="wave-1")
    late = _plan(db, proj, minter="GRPH-A1", wave="wave-2")
    db.flush()

    fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj, wave="wave-1")

    assert db.get(Enrolment, early.id).revoked is True
    assert db.get(Enrolment, late.id).revoked is False


# --- effect ------------------------------------------------------------------------


def test_seats_and_leases_go_together(db, proj):
    """A half-retired wave is the genuinely confusing state: work no living agent can
    finish, held by credentials that no longer authenticate."""
    seat = _plan(db, proj, minter="GRPH-A1")
    db.flush()
    agent = _agent_on(db, proj, seat)

    item = Item(id="GRPH-1", number=1, project_id=proj, title="t", status="in_progress",
                claimed_by=agent.id, claimed_at=datetime.now(timezone.utc), assignee="w")
    db.add(item)
    db.flush()  # the reservation's FK points at it
    db.add(AreaReservation(agent_id=agent.id, item_id=item.id, area="backend/app",
                           expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))
    db.flush()

    out = fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    assert out["seats_revoked"] == 1
    assert out["leases_released"] == 1
    assert out["reservations_released"] == 1

    fresh = db.get(Item, "GRPH-1")
    assert fresh.claimed_by is None
    assert fresh.status == "next", "an in-progress item must return to the queue"
    assert db.scalars(
        db.query(AreaReservation).filter_by(agent_id=agent.id).statement).all() == []


# --- and what it deliberately does NOT do ------------------------------------------


def test_it_names_the_processes_it_did_not_stop(db, proj):
    """`retire_wave` is a CREDENTIAL operation; `stop` is a PROCESS operation.

    Conflating them lets a planner call this, see success, and leave four agents
    building against revoked seats. `{"seats_revoked": 4}` on its own reads as "the wave
    is over" — so the count of children still executing is a returned number rather than
    a documented caveat.
    """
    seat = _plan(db, proj, minter="GRPH-A1")
    db.flush()
    live = _agent_on(db, proj, seat)

    out = fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    assert out["stopped_no_processes"] is True
    assert out["agents_still_running"] == [live.id]


def test_an_agent_already_gone_is_not_reported_as_still_running(db, proj):
    """The control. Without it `agents_still_running` could be every agent on the seat
    regardless, which is the same as no signal at all."""
    seat = _plan(db, proj, minter="GRPH-A1")
    db.flush()
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    _agent_on(db, proj, seat, seen=stale)

    out = fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    assert out["agents"] == 1, "the agent is still counted — it held the seat"
    assert out["agents_still_running"] == [], (
        "an agent whose presence lapsed is not a process the supervisor needs to stop"
    )


def test_retiring_nothing_says_so_rather_than_reading_as_success(db, proj):
    out = fleet.retire_wave(db, minter_id="GRPH-A-NOBODY", project_id=proj)
    assert out["seats_revoked"] == 0
    assert out["agents_still_running"] == []


# --- seeing the seats --------------------------------------------------------------


def test_a_planner_can_list_the_seats_it_minted_and_only_those(db, proj):
    mine = _plan(db, proj, minter="GRPH-A1")
    _plan(db, proj, minter="GRPH-A2")
    db.flush()

    rows = fleet.list_enrolments(db, project_id=proj, minted_by="GRPH-A1")

    assert [r["id"] for r in rows] == [mine.id]
    assert rows[0]["minted_by"] == "GRPH-A1"
    assert rows[0]["state"] == "unused"


def test_listing_shows_a_seat_becoming_revoked_rather_than_vanishing(db, proj):
    """The transition, not just its consequences. A planner watching an agent disappear
    could previously not tell a revoked seat from a crashed process."""
    seat = _plan(db, proj, minter="GRPH-A1")
    db.flush()
    assert fleet.list_enrolments(db, project_id=proj, minted_by="GRPH-A1")[0]["state"] == "unused"

    fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)

    rows = fleet.list_enrolments(db, project_id=proj, minted_by="GRPH-A1")
    assert len(rows) == 1, "a retired seat is a record, not a deletion"
    assert rows[0]["state"] == "revoked"


def test_no_part_of_a_seat_code_is_ever_returned(db, proj):
    """Asserted on the KEYS, not by hunting for substrings.

    The substring version was written first and failed immediately: two characters of
    `WORKER-7F3K` collide with a UUID by chance, so the test would have fired at random
    on a security property — and the pressure when that happens is to delete it. Pinning
    the field set is deterministic and catches the thing that would actually go wrong,
    which is somebody adding a `code_prefix` for the Fleet view.
    """
    seat, code = fleet.issue_enrolment(db, project_id=proj, role="worker", minted_by="GRPH-A1")
    db.flush()

    row = fleet.list_enrolments(db, project_id=proj, minted_by="GRPH-A1")[0]
    assert set(row) == {
        "id", "role", "wave", "state", "consumed_by", "minted_by", "reissued_from", "expires_at"
    }, "a new field on a seat row is a new chance to leak the code — say so deliberately"

    out = fleet.retire_wave(db, minter_id="GRPH-A1", project_id=proj)
    assert set(out) == {
        "seats_revoked", "agents", "leases_released", "reservations_released",
        "agents_still_running", "stopped_no_processes",
    }

    assert code not in repr(row) and code not in repr(out)


# --- the roster --------------------------------------------------------------------


def test_the_roster_carries_the_seat_not_just_whether_there_is_one(client, key, proj, db):
    """PRD-22 acceptance walk step 3 asks for two agents with DISTINCT enrolment_ids, and
    could not be run: `fleet_status` reported `enrolled` as a bare boolean. A supervisor
    also needs it to match a child it spawned to the roster row it became."""
    import json

    def _ok(tool, args):
        res = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}},
            headers={"X-API-Key": key}).json()["result"]
        assert not res.get("isError"), res
        return json.loads(res["content"][0]["text"])

    _, one = _seat(db, proj, "worker")
    _, two = _seat(db, proj, "reviewer")
    a = _ok("register_agent", {"label": "w", "enrolment_code": one})
    b = _ok("register_agent", {"label": "r", "enrolment_code": two})

    roster = {r["id"]: r for r in _ok("fleet_status", {})["agents"]}
    assert roster[a["agent_id"]]["enrolment_id"]
    assert roster[b["agent_id"]]["enrolment_id"]
    assert roster[a["agent_id"]]["enrolment_id"] != roster[b["agent_id"]]["enrolment_id"]
    # The boolean stays: the Fleet view groups un-enrolled agents apart and reads it.
    assert roster[a["agent_id"]]["enrolled"] is True
