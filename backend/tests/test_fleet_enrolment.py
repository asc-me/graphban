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


def test_list_enrolments_keeps_the_dead_seat_after_reissue(db, proj):
    """The Fleet view's roster is this list. Filtering it to unused seats after reissue
    would hide the consumed row — the audit trail reissue exists to leave behind."""
    dead, _ = _seat(db, proj, "worker")
    dead.consumed_at = datetime.now(timezone.utc)
    dead.consumed_by = "FA-A1"
    db.commit()

    fresh, _ = fleet.reissue_enrolment(db, enrolment_id=dead.id)

    by_id = {s["id"]: s for s in fleet.list_enrolments(db, proj)}
    assert by_id[dead.id]["state"] == "consumed", "the dead seat stays on the roster"
    assert by_id[fresh.id]["state"] == "unused"
    assert by_id[fresh.id]["reissued_from"] == dead.id


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


# ---- E3: independence derives from the session ----------------------------------------------

def _built_by(client, key, agent_id, title="work"):
    """An item in `review`, built by this agent — the state a reviewer acts on."""
    _ok(client, key, "create_item", {"title": title, "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": agent_id})
    _ok(client, key, "update_item",
        {"id": got["item"]["id"], "status": "review", "agent_id": agent_id})
    return got["item"]["id"]


def test_two_seats_on_one_credential_can_review_each_other(client, key, proj, db):
    """THE acceptance criterion, and the reason PRD-19 exists.

    One credential, no `instance` declared anywhere, no per-worktree config, no per-role key —
    the setup a client that stores a single MCP config is stuck with. Two seats make them two
    sessions, and the SERVER decided that rather than an agent claiming it."""
    _, wcode = _seat(db, proj, "worker")
    _, rcode = _seat(db, proj, "reviewer")
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": wcode})
    r = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": rcode})
    _built_by(client, key, w["agent_id"])

    out = _ok(client, key, "claim_review", {"agent_id": r["agent_id"]})

    assert out["claimed"] is True


def test_the_same_seat_twice_is_not_two_opinions(client, key, proj, db):
    """Defensive, and the reason single-use is a correctness property rather than caution: two
    agents on one enrolment are one session however they got there."""
    from app.models import Agent

    _, code = _seat(db, proj, "worker")
    a = _ok(client, key, "register_agent", {"label": "a", "enrolment_code": code})
    b = _ok(client, key, "register_agent", {"label": "b", "role_hint": "reviewer"})
    row = db.get(Agent, b["agent_id"])
    row.enrolment_id = db.get(Agent, a["agent_id"]).enrolment_id
    db.commit()

    assert fleet.independent(db.get(Agent, b["agent_id"]),
                             db.get(Agent, a["agent_id"])) is False


def test_an_enrolled_agent_beside_an_unenrolled_one_falls_back(client, key, proj, db):
    """An enrolment on one side proves nothing about the other — the un-enrolled process could
    be anything, including the same one twice. So the rule falls through to the declared
    discriminators, exactly as strict as before: neither declared anything, so no."""
    _, code = _seat(db, proj, "reviewer")
    w = _ok(client, key, "register_agent", {"label": "w"})
    r = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": code})
    _built_by(client, key, w["agent_id"])

    out = _ok(client, key, "claim_review", {"agent_id": r["agent_id"]})

    assert out["claimed"] is False


def test_the_fallback_still_works_when_only_one_is_enrolled(client, key, proj, db):
    """...and it is the SAME fallback, not a weaker one. A declared difference still earns
    independence when one side happens to hold a seat."""
    _, code = _seat(db, proj, "reviewer")
    w = _ok(client, key, "register_agent",
            {"label": "w", "capabilities": {"instance": "solo-1"}})
    r = _ok(client, key, "register_agent",
            {"label": "r", "enrolment_code": code, "capabilities": {"instance": "solo-2"}})
    _built_by(client, key, w["agent_id"])

    assert _ok(client, key, "claim_review", {"agent_id": r["agent_id"]})["claimed"] is True


def test_a_seat_does_not_launder_a_call_tree(client, key, proj, db):
    """Parentage is checked BEFORE the session and must stay that way. A subagent holding its
    own seat is still inside its parent's call tree — and D-g trap 2 keeps a planner from
    setting itself as parent on seats it mints, which would collapse the opposite way."""
    _, pcode = _seat(db, proj, "worker")
    _, ccode = _seat(db, proj, "reviewer")
    parent = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": pcode})
    child = _ok(client, key, "register_agent",
                {"label": "c", "enrolment_code": ccode,
                 "parent_agent_id": parent["agent_id"]})
    _built_by(client, key, parent["agent_id"])

    out = _ok(client, key, "claim_review", {"agent_id": child["agent_id"]})

    assert out["claimed"] is False, "a declared parent outranks two seats"


# ---- E4: issuing and tracking seats ----------------------------------------------------------

def test_a_wave_issues_one_seat_per_agent_including_repeats(client, auth, proj):
    """`["worker", "worker"]` is TWO seats, and the repeat is the point rather than a quirk of
    the API: two agents sharing a seat share a session and cannot review each other. An API
    that deduplicated roles would quietly provision a wave that cannot review itself."""
    out = client.post("/api/fleet/seats",
                      json={"project_id": proj, "wave": "w1",
                            "roles": ["planner", "worker", "worker", "reviewer"]},
                      headers=auth).json()["seats"]

    assert [s["role"] for s in out] == ["planner", "worker", "worker", "reviewer"]
    assert len({s["code"] for s in out}) == 4, "four seats, four codes"
    assert len({s["id"] for s in out}) == 4


def test_the_roster_call_reports_seat_state(client, auth, proj, key, db):
    """Read together with the roster because it is one question — "three agents online, one
    seat still unused" — and two calls would let the page render half of it."""
    codes = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    _ok(client, key, "register_agent", {"label": "w", "enrolment_code": codes[0]["code"]})

    seats = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["seats"]

    by_role = {s["role"]: s for s in seats}
    assert by_role["worker"]["state"] == "consumed"
    assert by_role["reviewer"]["state"] == "unused"


def test_the_roster_never_hands_a_code_back(client, auth, proj):
    """Shown once, like a key. A seat is short-lived and still a bearer token while it lives,
    and this endpoint is read by every agent on the project — not only by whoever issued it."""
    issued = client.post("/api/fleet/seats",
                         json={"project_id": proj, "wave": "w1", "roles": ["reviewer"]},
                         headers=auth).json()["seats"][0]

    out = client.get(f"/api/fleet?project_id={proj}", headers=auth)

    assert issued["code"] not in out.text
    # Not even a fragment — asserted on the FIELDS rather than by substring. An API key returns
    # a prefix because it is long-lived and must be matched against a config; a seat lives
    # thirty minutes and is named by role and wave, so exposing two characters of a six-
    # character code would shrink the search space for nothing.
    #
    # The substring version of this was FLAKY and CI caught it: two characters out of a
    # 31-symbol alphabet turn up inside UUIDs and timestamps by chance ("96", in a run where a
    # seat id contained `e03-9c06-79674138ea36`). A probabilistic assertion about a security
    # property is worse than none — it fails at random and gets weakened to make CI quiet.
    for seat in out.json()["seats"]:
        leaky = {k for k in seat if "code" in k or k == "fragment"}
        assert not leaky, f"the roster exposes {sorted(leaky)}"


def test_reissue_gives_a_fresh_code_and_keeps_the_dead_seat(client, auth, proj, key, db):
    """The recovery path for a crashed agent. The spent seat is NOT deleted — it is the record
    that something died, and the chain is how an operator sees it happened twice."""
    first = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"][0]
    _ok(client, key, "register_agent", {"label": "w", "enrolment_code": first["code"]})

    fresh = client.post(f"/api/fleet/seats/{first['id']}/reissue", headers=auth).json()

    assert fresh["code"] != first["code"] and fresh["reissued_from"] == first["id"]
    seats = {s["id"]: s for s in client.get(f"/api/fleet?project_id={proj}",
                                            headers=auth).json()["seats"]}
    assert seats[first["id"]]["state"] == "consumed", "the dead seat survives"
    assert seats[fresh["id"]]["state"] == "unused"


def test_a_reissued_seat_actually_works(client, auth, proj, key, db):
    """Asserting only on `state` would pass against a row that reads unused and is refused for
    some other reason — the same vacuity that let a sabotage through earlier in this PRD."""
    first = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["reviewer"]},
                        headers=auth).json()["seats"][0]
    fresh = client.post(f"/api/fleet/seats/{first['id']}/reissue", headers=auth).json()

    me = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": fresh["code"]})

    assert me["active_role"] == "reviewer" and me["enrolled"] is True


def test_issuing_no_roles_is_refused(client, auth, proj):
    """An empty wave is a mistake, not a wave of nothing."""
    r = client.post("/api/fleet/seats", json={"project_id": proj, "roles": []}, headers=auth)

    assert r.status_code == 422


# ---- E5: End wave expires sessions, not credentials -------------------------------------------

def _end_wave(client, auth, proj, wave="w1"):
    return client.post("/api/fleet/end-wave",
                       json={"project_id": proj, "wave": wave}, headers=auth).json()


def test_ending_a_wave_leaves_the_credential_authenticating(client, auth, proj, key, db):
    """THE property that lets a credential stop being per-wave, and the whole reason PRD-19
    is worth building. Before this, ending a wave revoked keys — so the config in every client
    had to be rewritten next time. Now the config is untouched and only the grant goes away."""
    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"]
    _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})

    _end_wave(client, auth, proj)

    # The same credential still works — it can still register, on a fresh seat.
    fresh = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w2", "roles": ["worker"]},
                        headers=auth).json()["seats"][0]
    me = _ok(client, key, "register_agent", {"label": "w2", "enrolment_code": fresh["code"]})
    assert me["active_role"] == "worker"


def test_an_expired_session_loses_its_role_without_losing_its_identity(client, auth, proj, key, db):
    """The seat is the grant, so revoking it removes the role — but `active_role` still records
    what the agent held, and the agent row stays. Rewriting the role would destroy the only
    account of what the fleet was doing when it was stopped."""
    from app.models import Agent

    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"]
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    _ok(client, key, "create_item", {"title": "x", "status": "next"})

    _end_wave(client, auth, proj)

    res = _rpc(client, key, "claim_next", {"agent_id": me["agent_id"]})
    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "enrolment" in res["structuredContent"]["error"]["message"]
    assert db.get(Agent, me["agent_id"]).active_role == "worker", "the record survives"


def test_the_agent_hears_about_it_on_its_next_poll(client, auth, proj, key, db):
    """Over the EXISTING downlink — no push, no SSE, no new transport. D6's whole claim was
    that intent travels on whatever the agent polls next, and this is the second use of it."""
    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"]
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})

    _end_wave(client, auth, proj)

    polled = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})
    assert polled["directive"]["type"] == "session_expired"
    assert "register_agent" in polled["directive"]["next"]


def test_the_expiry_directive_repeats_because_it_is_a_state(client, auth, proj, key, db):
    """A role change is an EVENT and is delivered once — redelivering would have an agent
    re-adopt a role it already holds. An expired session is a STATE that stays true until the
    agent re-enrols, so every poll must keep saying so. Acking it once would leave a stuck
    agent hearing nothing while every call it makes is refused."""
    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"]
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    _end_wave(client, auth, proj)

    first = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})
    second = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})

    assert first["directive"]["type"] == "session_expired"
    assert second["directive"]["type"] == "session_expired", "still true, still said"


def test_ending_a_wave_releases_what_the_seats_held(client, auth, proj, key, db):
    """All of it at once. A half-ended wave — grants revoked but leases still held — is the
    genuinely confusing state: work no living agent can finish, and nothing explaining why."""
    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "wave": "w1", "roles": ["worker"]},
                        headers=auth).json()["seats"]
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    _ok(client, key, "create_item", {"title": "held", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})

    out = _end_wave(client, auth, proj)

    assert out["seats_revoked"] == 1 and out["leases_released"] == 1
    from app.models import Item
    assert db.get(Item, got["item"]["id"]).claimed_by is None


def test_the_preview_names_the_seats_it_will_revoke(client, auth, proj, key, db):
    """A confirm that says "are you sure?" teaches people to click through it. The preview and
    the act share ONE selector, so the number named is the number delivered."""
    client.post("/api/fleet/seats",
                json={"project_id": proj, "wave": "w1", "roles": ["worker", "reviewer"]},
                headers=auth)

    preview = client.get(f"/api/fleet/end-wave?project_id={proj}&wave=w1", headers=auth).json()
    acted = _end_wave(client, auth, proj)

    assert preview["seats"] == 2
    assert acted["seats_revoked"] == preview["seats"]


def test_an_unenrolled_agent_is_untouched_by_ending_a_wave(client, auth, proj, key, db):
    """The single-agent posture is not part of any wave. Stopping it would make End wave a
    button that halts the developer's own agent, which it never promised."""
    me = _ok(client, key, "register_agent", {"label": "solo"})
    _ok(client, key, "create_item", {"title": "mine", "status": "next"})

    _end_wave(client, auth, proj)

    out = _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    assert out["claimed"] is True


# ---- E7: a planner may mint seats -------------------------------------------------------------

def test_a_planner_can_mint_a_seat_for_an_agent_it_spawns(client, key, proj, db):
    """An orchestrator cannot paste a code out of a UI, so without this an autonomous fleet is
    impossible — every seat would need a human at the Fleet view."""
    _, pcode = _seat(db, proj, "planner")
    boss = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": pcode})

    out = _ok(client, key, "mint_enrolment",
              {"agent_id": boss["agent_id"], "role": "worker"})

    assert out["role"] == "worker" and out["enrolment_code"].startswith("WORKER-")
    hand = _ok(client, key, "register_agent",
               {"label": "w", "enrolment_code": out["enrolment_code"]})
    assert hand["active_role"] == "worker" and hand["enrolled"] is True


def test_a_worker_cannot_mint(client, key, proj, db):
    """THE containment, and it is structural rather than a check beside the capability. A
    worker that could mint would build an item, mint itself a reviewer seat, register as a
    fresh agent — new id, new enrolment, therefore independent — and sign off its own work,
    invisibly to an authorship ban keyed on agent id."""
    _, wcode = _seat(db, proj, "worker")
    me = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": wcode})

    res = _rpc(client, key, "mint_enrolment", {"agent_id": me["agent_id"], "role": "reviewer"})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"


def test_minting_stays_planner_only(client, key, proj, db):
    """Asserted on the ROLE MAP, not just by exercising a worker. The safety argument is
    "planners cannot build, so they have nothing to launder" — adding a second role to this
    entry would evaporate that argument silently, and the tool would keep working."""
    assert fleet.TOOL_ROLES["mint_enrolment"] == ("planner",)
    # And the half the argument rests on: a planner genuinely cannot claim work.
    assert "worker" in fleet.TOOL_ROLES["claim_next"]
    assert "planner" not in fleet.TOOL_ROLES["claim_next"]


def test_a_minted_seat_records_its_minter_and_sets_no_parentage(client, key, proj, db):
    """D-g trap 2, and it would break the feature outright rather than subtly. Recording the
    minter as the parent is the intuitive move — and `independent` treats siblings under one
    parent as one call tree, so every seat a planner issued would be mutually non-independent
    and no agent in an autonomous fleet could review any other."""
    from app.models import Agent, Enrolment as E

    _, pcode = _seat(db, proj, "planner")
    boss = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": pcode})
    out = _ok(client, key, "mint_enrolment", {"agent_id": boss["agent_id"], "role": "worker"})

    seat = db.get(E, out["seat_id"])
    assert seat.minted_by == boss["agent_id"]
    hand = _ok(client, key, "register_agent",
               {"label": "w", "enrolment_code": out["enrolment_code"]})
    assert db.get(Agent, hand["agent_id"]).parent_agent_id is None


def test_two_agents_a_planner_seated_can_review_each_other(client, key, proj, db):
    """The acceptance criterion for autonomous provisioning (PRD-19 §9.8). If parentage were
    recorded on minted seats, this is the test that would fail — and the fleet would be unable
    to review anything it built."""
    _, pcode = _seat(db, proj, "planner")
    boss = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": pcode})
    w = _ok(client, key, "mint_enrolment", {"agent_id": boss["agent_id"], "role": "worker"})
    r = _ok(client, key, "mint_enrolment", {"agent_id": boss["agent_id"], "role": "reviewer"})

    worker = _ok(client, key, "register_agent",
                 {"label": "w", "enrolment_code": w["enrolment_code"]})
    reviewer = _ok(client, key, "register_agent",
                   {"label": "r", "enrolment_code": r["enrolment_code"]})
    _built_by(client, key, worker["agent_id"])

    assert _ok(client, key, "claim_review",
               {"agent_id": reviewer["agent_id"]})["claimed"] is True


def test_a_planner_cannot_mint_past_its_own_credential(client, auth, proj, db):
    """The credential is still the ceiling. A planner reshuffles within what it holds; it does
    not manufacture authority its own key was never granted."""
    narrow = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "planner", "wave": "w1"},
                         headers=auth).json()["plaintext"]
    me = _ok(client, narrow, "register_agent", {"label": "p", "role_hint": "planner"})

    res = _rpc(client, narrow, "mint_enrolment",
               {"agent_id": me["agent_id"], "role": "reviewer"})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "reviewer" in res["structuredContent"]["error"]["message"]


# ---- found on the PRD-17 acceptance walk, 2026-08-13 -------------------------------------------

def test_update_item_advertises_agent_id(client):
    """THE bug, and the reason a green suite meant nothing: `update_item` did not advertise
    `agent_id`, so `check_tool_role` always saw None, fell back to the KEY's ceiling, and an
    unrestricted credential resolved to "no restriction". A real worker wrote `done` on the
    walk — correctly, given what the server was told.

    Every test that "proved" the ceiling passed `agent_id` by hand, which is a parameter the
    published schema forbade. They exercised a path no client could reach."""
    from app.mcp_server import TOOLS

    props = next(t for t in TOOLS if t["name"] == "update_item")["inputSchema"]["properties"]
    assert "agent_id" in props


def test_a_worker_cannot_write_done_by_omitting_its_own_id(client, key, proj, db):
    """The bypass itself. Identifying yourself must not be optional when it is the only thing
    standing between a worker and `done` — absence has to read as "unknown", never as
    "unrestricted"."""
    _, wcode = _seat(db, proj, "worker")
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": wcode})
    _ok(client, key, "create_item", {"title": "x", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": w["agent_id"]})

    res = _rpc(client, key, "update_item", {"id": got["item"]["id"], "status": "done"})

    assert res.get("isError") is True, "an anonymous call must not inherit the key's ceiling"
    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    from app.models import Item
    assert db.get(Item, got["item"]["id"]).status != "done"


def test_a_lone_unregistered_agent_still_works(client, auth, proj):
    """The other half, and the reason this is not simply "always require agent_id". A single
    developer with one agent and no registration is the DEFAULT posture — refusing it would
    break every setup predating PRD-17 to fix a fleet problem."""
    raw = client.post("/api/api-keys", json={"name": "solo", "project_id": proj},
                      headers=auth).json()["plaintext"]
    _ok(client, raw, "create_item", {"title": "mine", "status": "next"})
    got = _ok(client, raw, "claim_next", {})

    out = _ok(client, raw, "update_item", {"id": got["item"]["id"], "status": "done"})

    assert out["status"] == "done"


def test_every_role_can_keep_itself_alive(client, key, proj, db):
    """`heartbeat` was gated to ("worker",), so a reviewer or planner was refused the only call
    that keeps it on the roster. Both registered fine and vanished 150s later with their
    terminals open — observed on the walk as `role_refused ... heartbeat` for each."""
    for role in ("planner", "reviewer"):
        _, code = _seat(db, proj, role)
        me = _ok(client, key, "register_agent", {"label": role, "enrolment_code": code})

        out = _ok(client, key, "heartbeat", {"agent_id": me["agent_id"]})

        assert out["agent_id"] == me["agent_id"]


def test_heartbeat_needs_no_item(client, key, proj, db):
    """It required one, so presence was maintainable only while mid-work. A planner never holds
    an item at all; a reviewer between reviews and a worker between claims hold none either —
    the exact agents the roster is asked about."""
    from app.mcp_server import TOOLS

    schema = next(t for t in TOOLS if t["name"] == "heartbeat")["inputSchema"]
    assert "id" not in schema.get("required", [])
    _, code = _seat(db, proj, "planner")
    me = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": code})

    out = _ok(client, key, "heartbeat", {"agent_id": me["agent_id"]})

    assert out["presence_ttl_seconds"] > 0


# ---- waves actually increment (GRPH-378) -------------------------------------------------------

def test_each_wave_gets_its_own_number(client, auth, proj):
    """The Fleet view hardcoded `wave-1`, so every wave since PRD-17 landed in one bucket —
    19 seats and 15 keys deep before anyone noticed. End wave therefore always ended
    EVERYTHING, and two waves could never run side by side.

    Computed server-side on purpose: a number the UI has to remember to increment is a number
    that stays 1."""
    first = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                        headers=auth).json()
    second = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                         headers=auth).json()

    assert first["wave"] == "wave-1"
    assert second["wave"] == "wave-2"


def test_the_next_wave_steps_over_a_legacy_key_wave(client, auth, proj):
    """A wave owns seats now and owned KEYS before PRD-19. Reusing a label from either would
    let End wave reach back into a cohort somebody already finished with."""
    client.post("/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "wave-7"},
                headers=auth)

    out = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                      headers=auth).json()

    assert out["wave"] == "wave-8"


def test_an_explicit_wave_is_still_honoured(client, auth, proj):
    out = client.post("/api/fleet/seats",
                      json={"project_id": proj, "roles": ["worker"], "wave": "hotfix"},
                      headers=auth).json()
    assert out["wave"] == "hotfix"


def test_revoking_unused_seats_leaves_the_consumed_ones(client, auth, proj, key, db):
    """A consumed seat is the record of which agent took what. Clearing leftovers must not
    erase that — and it must not stop a live agent either; ending the wave is what does."""
    issued = client.post("/api/fleet/seats",
                         json={"project_id": proj, "roles": ["worker", "worker", "reviewer"]},
                         headers=auth).json()["seats"]
    me = _ok(client, key, "register_agent",
             {"label": "w", "enrolment_code": issued[0]["code"]})

    out = client.post("/api/fleet/seats/revoke-unused",
                      json={"project_id": proj}, headers=auth).json()

    assert out["revoked"] == 2
    seats = {s["id"]: s for s in client.get(f"/api/fleet?project_id={proj}",
                                            headers=auth).json()["seats"]}
    assert seats[issued[0]["id"]]["state"] == "consumed", "the redeemed seat survives"
    # And the agent that holds it is untouched.
    assert _ok(client, key, "heartbeat", {"agent_id": me["agent_id"]})["agent_id"] == me["agent_id"]


def test_the_roster_lists_credentials_without_key_material(client, auth, proj, key):
    """The walk kept asking "which key is that agent on" and the answer lived on another
    screen. Shown here — as the display prefix only, never anything usable."""
    body = client.get(f"/api/fleet?project_id={proj}", headers=auth)
    creds = body.json()["credentials"]

    assert creds and all("prefix" in c and "hashed_key" not in c for c in creds)
    assert key not in body.text


# ---- the wave selector offers only what is live (GRPH-379) --------------------------------------

def test_only_waves_that_still_own_something_are_offered(client, auth, proj, key, db):
    """On the walk the selector listed three waves, none of which had a single live seat
    between them. Ending history is not an action — it is noise on a destructive control."""
    w1 = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                     headers=auth).json()
    w2 = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                     headers=auth).json()
    assert (w1["wave"], w2["wave"]) == ("wave-1", "wave-2")

    assert client.get(f"/api/fleet?project_id={proj}",
                      headers=auth).json()["waves"] == ["wave-2", "wave-1"]

    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "wave-1"}, headers=auth)

    assert client.get(f"/api/fleet?project_id={proj}",
                      headers=auth).json()["waves"] == ["wave-2"]


def test_a_consumed_seat_keeps_its_wave_live(client, auth, proj, key, db):
    """Live means "owns something un-revoked", not "has seats nobody took". A wave whose seats
    are all consumed is the NORMAL running state — that is a fleet at work, and the one you are
    most likely to want to end."""
    issued = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"]},
                         headers=auth).json()
    _ok(client, key, "register_agent", {"label": "w", "enrolment_code": issued["seats"][0]["code"]})

    assert issued["wave"] in client.get(f"/api/fleet?project_id={proj}",
                                        headers=auth).json()["waves"]


def test_revoking_expired_keys_spares_the_ones_still_good(client, auth, proj, db):
    """EXPIRED only. "Unused" would be the tempting second signal and is a trap: a key minted
    minutes ago for a machine nobody has set up yet has never been used, and sweeping on that
    would revoke an operator's own setup before they finished it."""
    from app.models import ApiKey

    dead = client.post("/api/api-keys", json={"name": "old", "project_id": proj},
                       headers=auth).json()["id"]
    fresh = client.post("/api/api-keys", json={"name": "new", "project_id": proj},
                        headers=auth).json()["id"]
    row = db.get(ApiKey, dead)
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    out = client.post("/api/fleet/keys/revoke-expired", json={"project_id": proj},
                      headers=auth).json()

    assert out["revoked"] == 1
    db.expire_all()
    assert db.get(ApiKey, dead).revoked is True
    assert db.get(ApiKey, fresh).revoked is False, "a never-used key is not a dead one"


# ---- authorship outlives the lease (GRPH-377 / GRPH-376) ---------------------------------------

def test_ending_a_wave_does_not_let_an_agent_review_its_own_work(client, auth, proj, key, db):
    """THE hole, in the exact sequence the walk produced. End wave releases every lease its
    agents hold and only resets status for `in_progress` — so an item in REVIEW kept that
    status and lost its author. `independent(reviewer, None)` then reads "human-authored,
    nothing to be independent of", and the agent that built the work could sign it off.

    An enforcement input removed by a routine operation, while the work was still in flight."""
    from app.models import Item

    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    _ok(client, key, "create_item", {"title": "mine", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": w["agent_id"]})
    item_id = got["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item_id, "status": "review", "agent_id": w["agent_id"]})

    client.post("/api/fleet/end-wave", json={"project_id": proj}, headers=auth)

    # The lease is gone, as it should be. The AUTHOR is not.
    db.expire_all()
    row = db.get(Item, item_id)
    assert row.claimed_by is None, "the lease is correctly released"
    assert row.built_by == w["agent_id"], "authorship survives the wave that released it"

    # And the ban still bites for the agent that built it.
    res = _rpc(client, key, "sign_off", {"id": item_id, "agent_id": w["agent_id"]})
    assert res.get("isError") is True
    assert res["structuredContent"]["error"]["code"] == "unauthorized"

    # RESIDUAL, and it is the session model rather than this bug: the same PROCESS registered
    # afresh on a NEW seat is a new session, and D-d makes two seats independent by
    # construction. The server cannot tell that process from any other — which is the trade
    # PRD-19 made deliberately, and why enrolment is called coordination rather than a
    # boundary. What is fixed here is that authorship no longer VANISHES; who may act on it is
    # a separate question with a stated answer.


def test_signing_off_keeps_the_record_of_who_built_it(client, auth, proj, key, db):
    """GRPH-376: `sign_off` released the lease and the audit trail went with it, so every done
    item read `built_by: -` and PRD-17's own criterion — reviewed_by != claimed_by — was
    unverifiable for exactly the items it describes."""
    from app.models import Item

    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    r = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": seats[1]["code"]})
    _ok(client, key, "create_item", {"title": "work", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": w["agent_id"]})
    _ok(client, key, "update_item",
        {"id": got["item"]["id"], "status": "review", "agent_id": w["agent_id"]})
    _ok(client, key, "claim_review", {"agent_id": r["agent_id"]})
    _ok(client, key, "sign_off", {"id": got["item"]["id"], "agent_id": r["agent_id"]})

    db.expire_all()
    row = db.get(Item, got["item"]["id"])
    assert row.status == "done"
    assert row.built_by == w["agent_id"] and row.reviewed_by == r["agent_id"]
    assert row.built_by != row.reviewed_by, "the criterion is checkable after the fact"


def test_a_bounce_pins_to_the_author_not_the_lease(client, auth, proj, key, db):
    """`bounce` pins the item to whoever BUILT it for one lease. Reading that from the lease
    meant a bounce after an End wave pinned to nobody — the pin existed and pointed at no one,
    so step 9's "invisible to other workers until the pin lapses" could not hold."""
    from app.models import Item

    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    r = _ok(client, key, "register_agent", {"label": "r", "enrolment_code": seats[1]["code"]})
    _ok(client, key, "create_item", {"title": "bounce me", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": w["agent_id"]})
    _ok(client, key, "update_item",
        {"id": got["item"]["id"], "status": "review", "agent_id": w["agent_id"]})
    _ok(client, key, "claim_review", {"agent_id": r["agent_id"]})

    _ok(client, key, "bounce", {"id": got["item"]["id"], "agent_id": r["agent_id"],
                                "reason": "needs a test"})

    db.expire_all()
    row = db.get(Item, got["item"]["id"])
    assert row.status == "next"
    assert row.bounce_pinned_to == w["agent_id"], "pinned to the author, who can fix it"
    assert row.built_by == w["agent_id"], "and still recorded as its author"


def test_a_subagent_cannot_sign_its_parents_work_after_a_wave_ends(client, auth, proj, key, db):
    """The half the identity check does NOT cover, and the one that made a sabotage pass. The
    `built_by == agent_id` guard catches the author itself; `independent()` catches everyone in
    the same call tree. Resolve the author from the LEASE and, after an End wave has released
    it, independence is computed against None — so a subagent signs off its parent's work."""
    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    parent = _ok(client, key, "register_agent", {"label": "p", "enrolment_code": seats[0]["code"]})
    _ok(client, key, "create_item", {"title": "parent work", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": parent["agent_id"]})
    _ok(client, key, "update_item",
        {"id": got["item"]["id"], "status": "review", "agent_id": parent["agent_id"]})
    client.post("/api/fleet/end-wave", json={"project_id": proj}, headers=auth)

    # The child registers AFTER the wave, on a seat that is still valid. Registering it before
    # made this test vacuous: End wave revoked its seat too, so it was refused for SESSION
    # EXPIRY and never reached the independence check the test exists to exercise. A sabotage
    # that resolved the author from the released lease passed against that version.
    fresh = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["reviewer"]},
                        headers=auth).json()["seats"][0]
    child = _ok(client, key, "register_agent",
                {"label": "c", "enrolment_code": fresh["code"],
                 "parent_agent_id": parent["agent_id"]})

    res = _rpc(client, key, "sign_off",
               {"id": got["item"]["id"], "agent_id": child["agent_id"]})

    assert res.get("isError") is True, "a call tree cannot review itself, wave or no wave"


def test_a_bounce_after_a_wave_ends_still_pins_to_the_author(client, auth, proj, key, db):
    """`bounce` reads the author to pin the item to whoever can fix it. Read from the LEASE and
    a bounce after an End wave pins to nobody — the pin exists, points at no one, and step 9's
    "invisible to other workers until it lapses" is quietly false."""
    from app.models import Item

    seats = client.post("/api/fleet/seats",
                        json={"project_id": proj, "roles": ["worker", "reviewer"]},
                        headers=auth).json()["seats"]
    w = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": seats[0]["code"]})
    _ok(client, key, "create_item", {"title": "bounce after wave", "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": w["agent_id"]})
    _ok(client, key, "update_item",
        {"id": got["item"]["id"], "status": "review", "agent_id": w["agent_id"]})
    client.post("/api/fleet/end-wave", json={"project_id": proj}, headers=auth)

    fresh = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["reviewer"]},
                        headers=auth).json()["seats"][0]
    r = _ok(client, key, "register_agent", {"label": "r2", "enrolment_code": fresh["code"]})
    _ok(client, key, "bounce", {"id": got["item"]["id"], "agent_id": r["agent_id"],
                                "reason": "still needs a test"})

    db.expire_all()
    assert db.get(Item, got["item"]["id"]).bounce_pinned_to == w["agent_id"]


# ---- dismissing an agent (GRPH-380) ------------------------------------------------------------

def test_dismissing_hides_an_agent_without_deleting_it(client, auth, proj, key, db):
    """A roster that only grows is one nobody reads — a day of walking left 24 rows of which 16
    were dead processes holding nothing.

    HIDDEN, NEVER DELETED. `Item.claimed_by`, `reviewed_by` and `built_by` hold agent ids as
    plain strings, so removing a row dangles them silently; and `keys.mint` allocates
    max(number)+1, so a freed number would let one id name two different agents at different
    times. Everything that made authorship worth preserving in 0067 makes deletion wrong here."""
    from app.models import Agent

    me = _ok(client, key, "register_agent", {"label": "spent"})

    out = client.post(f"/api/fleet/agents/{me['agent_id']}/dismiss",
                      json={}, headers=auth).json()

    assert out["dismissed"] is True
    row = db.get(Agent, me["agent_id"])
    assert row is not None, "the row survives — durable work references this id"
    assert row.dismissed_at is not None


def test_an_agent_still_holding_work_refuses_to_be_dismissed(client, auth, proj, key, db):
    """The one case where hiding costs something. An agent holding a lease is unfinished
    business — the exact thing the roster exists to surface — and dismissing it would take the
    work out of view along with it."""
    me = _ok(client, key, "register_agent", {"label": "busy"})
    _ok(client, key, "create_item", {"title": "held", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})

    r = client.post(f"/api/fleet/agents/{me['agent_id']}/dismiss", json={}, headers=auth)

    assert r.status_code == 409
    assert "still holds" in r.json()["detail"]


def test_an_orphaned_branch_also_refuses(client, auth, proj, key, db):
    """The other unfinished business, and the one only a human can resolve. The fleet releases
    the ITEM by itself; the branch it left behind is why the row must stay visible."""
    from app.models import Agent

    me = _ok(client, key, "register_agent", {"label": "orphan"})
    row = db.get(Agent, me["agent_id"])
    row.branch_orphaned = True
    db.commit()

    r = client.post(f"/api/fleet/agents/{me['agent_id']}/dismiss", json={}, headers=auth)

    assert r.status_code == 409
    assert "branch" in r.json()["detail"]


def test_a_dismissal_can_be_undone(client, auth, proj, key, db):
    """Hiding is a view decision, not a verdict — and a mis-click on a roster of two dozen
    should not be permanent."""
    me = _ok(client, key, "register_agent", {"label": "oops"})
    client.post(f"/api/fleet/agents/{me['agent_id']}/dismiss", json={}, headers=auth)

    out = client.post(f"/api/fleet/agents/{me['agent_id']}/dismiss",
                      json={"undo": True}, headers=auth).json()

    assert out["dismissed"] is False


def test_the_roster_reports_enrolment_and_dismissal(client, auth, proj, key, db):
    """The view groups un-enrolled agents apart — they are the single-agent posture, which is
    legitimate but is not a fleet — so it has to be told which is which."""
    _, code = _seat(db, proj, "worker")
    seated = _ok(client, key, "register_agent", {"label": "w", "enrolment_code": code})
    solo = _ok(client, key, "register_agent", {"label": "solo"})

    rows = {a["id"]: a for a in fleet.list_agents(db, proj)}

    assert rows[seated["agent_id"]]["enrolled"] is True
    assert rows[solo["agent_id"]]["enrolled"] is False
    assert all(a["dismissed"] is False for a in rows.values())
