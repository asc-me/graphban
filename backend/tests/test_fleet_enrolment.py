"""E1/E2 — enrolment seats: a role for one session, separate from the credential (PRD-19).

An MCP client sends ONE static header, so welding the role into it forced a credential per
role — and a client that stores one config for every agent cannot then run a fleet at all.
A seat is the ephemeral half: one role, one project, one session, expiring.

Three properties carry this, and none of them is the happy path.

**A refused seat must not burn.** The ceiling is checked before consumption, so an operator who
mints the wrong pairing can fix the credential and retry with the code already handed out. A
seat spent on a refusal is unrecoverable — codes are shown once.

**A seat is single-use, and that is correctness rather than caution.** Two agents redeeming one
code share an enrolment, and PRD-19 D-d makes independence derive from the enrolment — so a
reused code silently disables review between the two agents that used it, inside a wave that
looks correctly provisioned.

**A ceiling conflict is refused, never narrowed.** Clamping a reviewer seat to `worker` leaves
the roster showing a worker where a reviewer was deliberately issued — the one state an
operator cannot debug from the UI.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Enrolment
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
    return client.post("/api/projects", json={"name": "Enrolment"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    """ONE shared credential — the whole point. Every agent below authenticates with it."""
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _seat(db, proj, role, **kw):
    return fleet.issue_enrolment(db, project_id=proj, role=role, **kw)


# ---- issuing --------------------------------------------------------------------------------

def test_a_seat_is_stored_hashed_and_the_code_is_returned_once(db, proj):
    """A seat is short-lived and still a bearer token for the minutes it lives."""
    row, code = _seat(db, proj, "worker")

    assert code.startswith("WORKER-") and len(code.split("-")[1]) == 6
    assert row.code_hash != code and code not in row.code_hash
    assert db.scalar(
        __import__("sqlalchemy").select(Enrolment).where(Enrolment.code_hash == row.code_hash))


def test_two_seats_for_one_role_are_different_seats(db, proj):
    """THE property behind "a seat, not a role". Two workers need two codes: agents sharing an
    enrolment share a session, and D-d then makes them non-independent — so issuing one code
    per ROLE would silently disable review between a wave's own workers."""
    a, code_a = _seat(db, proj, "worker")
    b, code_b = _seat(db, proj, "worker")

    assert a.id != b.id and code_a != code_b


def test_a_fresh_seat_is_unused_and_expires_within_the_ttl(db, proj):
    row, _ = _seat(db, proj, "reviewer")

    assert fleet.enrolment_state(row) == "unused"
    remaining = row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(minutes=fleet.ENROLMENT_TTL_MINUTES - 1) < remaining <= timedelta(
        minutes=fleet.ENROLMENT_TTL_MINUTES)


def test_reissue_replaces_a_dead_seat_and_points_back_at_it(db, proj):
    """The recovery path, and the reason no `max_uses` is needed: a crashed agent is given a
    NEW seat and the dead one survives as the record that something died."""
    dead, _ = _seat(db, proj, "worker")

    fresh, code = fleet.reissue_enrolment(db, enrolment_id=dead.id)

    assert fresh.id != dead.id and fresh.reissued_from == dead.id
    assert fresh.role == dead.role and code.startswith("WORKER-")


# ---- redeeming ------------------------------------------------------------------------------

def test_a_seat_grants_its_role_on_a_shared_credential(client, key, proj, db):
    """G2: the role is ENFORCED on a credential that permits everything, because the server
    issued the grant rather than the agent asserting it."""
    _, code = _seat(db, proj, "reviewer")

    me = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": code})

    assert me["active_role"] == "reviewer"
    assert me["enrolled"] is True


def test_the_seat_beats_a_conflicting_role_hint(client, key, proj, db):
    """Two sources for one fact is how the role came to be self-declared. The seat wins and
    the hint is ignored, not merged."""
    _, code = _seat(db, proj, "reviewer")

    me = _ok(client, key, "register_agent",
             {"label": "r", "role_hint": "worker", "enrolment_code": code})

    assert me["active_role"] == "reviewer"


def test_registering_without_a_seat_is_all_in_one_and_says_so(client, key):
    """G5/D-c: the default costs nothing, and `enrolled` is STATED rather than inferred —
    `all-in-one` is both a grantable seat role and what an un-enrolled agent gets, so a client
    cannot tell the deliberate case from the forgotten one without being told."""
    me = _ok(client, key, "register_agent", {"label": "solo"})

    assert me["active_role"] == fleet.ALL_IN_ONE
    assert me["enrolled"] is False


def test_a_seat_cannot_be_redeemed_twice(client, key, proj, db):
    """Single use is correctness, not caution: two agents on one enrolment cannot review each
    other, so a reused code disables review inside a wave that looks correctly provisioned."""
    _, code = _seat(db, proj, "worker")
    _ok(client, key, "register_agent", {"label": "first", "enrolment_code": code})

    res = _rpc(client, key, "register_agent", {"label": "second", "enrolment_code": code})

    assert res.get("isError") is True
    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "consumed" in res["structuredContent"]["error"]["message"]


def test_an_expired_seat_is_refused(client, key, proj, db):
    row, code = _seat(db, proj, "worker")
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    res = _rpc(client, key, "register_agent", {"label": "late", "enrolment_code": code})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "expired" in res["structuredContent"]["error"]["message"]


def test_an_unknown_code_is_refused(client, key):
    res = _rpc(client, key, "register_agent",
               {"label": "x", "enrolment_code": "WORKER-ZZZZZZ"})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"


def test_a_seat_from_another_project_is_refused(client, auth, key, proj, db):
    """A seat is scoped to the project that issued it. Redeeming across projects would let a
    credential with two project scopes borrow a role it was never granted there."""
    other = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    _, code = _seat(db, other, "reviewer")

    res = _rpc(client, key, "register_agent", {"label": "x", "enrolment_code": code})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "different project" in res["structuredContent"]["error"]["message"]


# ---- the ceiling ----------------------------------------------------------------------------

def test_a_seat_cannot_grant_a_role_the_credential_forbids(client, auth, proj, db):
    """The credential stays the ceiling. Otherwise a seat would be a way to exceed the
    authorization the operator actually granted."""
    narrow = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "worker", "wave": "w1"},
                         headers=auth).json()["plaintext"]
    _, code = _seat(db, proj, "reviewer")

    res = _rpc(client, narrow, "register_agent", {"label": "x", "enrolment_code": code})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    msg = res["structuredContent"]["error"]["message"]
    assert "worker" in msg and "reviewer" in msg, "the refusal names both sides"


def test_a_refused_ceiling_does_not_burn_the_seat(client, auth, proj, db):
    """THE one that decides whether a mistake is recoverable. Codes are shown once, so a seat
    spent on a refusal is gone — the operator would have to reissue and re-paste a prompt they
    already distributed. The ceiling is checked BEFORE consumption for exactly this."""
    narrow = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "worker", "wave": "w1"},
                         headers=auth).json()["plaintext"]
    row, code = _seat(db, proj, "reviewer")
    _rpc(client, narrow, "register_agent", {"label": "x", "enrolment_code": code})

    db.refresh(row)
    assert fleet.enrolment_state(row) == "unused", "the refused seat is still redeemable"

    # And it genuinely still works, which is the claim. Asserting only on the state would pass
    # against a seat marked unused but rejected for some other reason on the retry.
    wide = client.post("/api/api-keys", json={"name": "wide", "project_id": proj},
                       headers=auth).json()["plaintext"]
    me = _ok(client, wide, "register_agent", {"label": "retry", "enrolment_code": code})
    assert me["active_role"] == "reviewer"


def test_a_refused_registration_mints_no_agent(client, key, proj, db):
    """A refusal must leave nothing behind. An agent row minted for a registration that then
    failed would appear on the roster having never connected, and `keys.mint` would have
    consumed a number that names a process which does not exist."""
    from app.models import Agent

    before = db.query(Agent).count()
    _rpc(client, key, "register_agent", {"label": "x", "enrolment_code": "WORKER-ZZZZZZ"})

    assert db.query(Agent).count() == before


def test_the_agent_records_which_seat_it_consumed(client, key, proj, db):
    """E3 reads this to decide independence, and the seat records the agent for the audit
    trail a reissue leaves behind. Both halves of the link are written in one transaction."""
    from app.models import Agent

    row, code = _seat(db, proj, "worker")
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": code})

    db.refresh(row)
    assert db.get(Agent, me["agent_id"]).enrolment_id == row.id
    assert row.consumed_by == me["agent_id"]
    assert fleet.enrolment_state(row) == "consumed"
