"""Declining an item must be possible, and must not make you its author (GRPH-429, GRPH-434).

Both were found by trying to use the fleet rather than by reading it.

A reviewer was handed its own work by `claim_review`, could not hand it back — `release_item`
was worker-only — and every subsequent call returned the same item, so the other twenty in the
queue were unreachable. From the worker side, where `release_item` IS available, it made no
difference: eight claim/release cycles returned the same item eight times, because a released
item is immediately top-scored again.

And each of those claims stamped `built_by`. An agent that never opened an item ended up
recorded as its author, which `independent()` then reads as a reason to bar it from reviewing
what it declined.
"""
import pytest

from app.models import Item
from app.services import fleet
from app.services import items as items_svc


def _ok(client, key, tool, args=None):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args or {}}},
                    headers={"X-API-Key": key}).json()
    import json as _json
    return _json.loads(r["result"]["content"][0]["text"])


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Declining"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seat(client, auth, proj, role):
    return client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": [role], "wave": "w1"},
                       headers=auth).json()["seats"][0]["code"]


# ---- skipping: the queue can be walked ------------------------------------------------------

def test_a_worker_can_pass_over_the_item_it_cannot_take(client, auth, proj, key):
    """Measured before the fix: claim, release, claim returned the SAME item eight times out of
    eight, because releasing puts it back at the top. An agent that cannot take the head of the
    queue could reach nothing behind it."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    first = _ok(client, key, "create_item", {"title": "needs production access", "status": "next"})
    second = _ok(client, key, "create_item", {"title": "the one I can do", "status": "next"})

    got = _ok(client, key, "claim_next", {"agent_id": me["agent_id"], "skip": [first["id"]]})

    assert got["claimed"] and got["item"]["id"] == second["id"]


def test_skipping_everything_is_an_empty_answer_not_a_wrong_one(client, auth, proj, key):
    """Declining the whole queue must read as "nothing for you", not hand back something you
    already refused."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    only = _ok(client, key, "create_item", {"title": "the only work", "status": "next"})

    got = _ok(client, key, "claim_next", {"agent_id": me["agent_id"], "skip": [only["id"]]})

    assert got["claimed"] is False


def test_a_reviewer_can_pass_over_its_own_work(client, auth, proj, key):
    """THE case. `claim_review` handed a reviewer the item it had built, and the server could
    not refuse it because `built_by` was null — nothing recorded an author, so `independent`
    returns True. The discipline is the reviewer's, so the surface has to let it act on one."""
    worker = _ok(client, key, "register_agent", {"label": "w",
                                                 "enrolment_code": _seat(client, auth, proj, "worker")})
    rev = _ok(client, key, "register_agent", {"label": "r",
                                              "enrolment_code": _seat(client, auth, proj, "reviewer")})
    mine = _ok(client, key, "create_item", {"title": "the reviewer's own", "status": "review"})
    theirs = _ok(client, key, "create_item", {"title": "somebody else's", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item", {"id": theirs["id"], "status": "review",
                                     "agent_id": worker["agent_id"]})

    got = _ok(client, key, "claim_review", {"agent_id": rev["agent_id"], "skip": [mine["id"]]})

    assert got["claimed"] and got["item"]["id"] == theirs["id"]


# ---- releasing: a hold is a hold, whichever one you have -------------------------------------

def test_a_reviewer_can_hand_back_a_review_claim(client, auth, proj, key, db):
    """`release_item` was worker-only, so a reviewer holding an item it would not judge had no
    exit at all — it waited out a 600s lease while the queue handed it the same item."""
    worker = _ok(client, key, "register_agent", {"label": "w",
                                                 "enrolment_code": _seat(client, auth, proj, "worker")})
    rev = _ok(client, key, "register_agent", {"label": "r",
                                              "enrolment_code": _seat(client, auth, proj, "reviewer")})
    item = _ok(client, key, "create_item", {"title": "work", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item", {"id": item["id"], "status": "review",
                                     "agent_id": worker["agent_id"]})
    took = _ok(client, key, "claim_review", {"agent_id": rev["agent_id"]})
    assert took["claimed"]

    out = _ok(client, key, "release_item", {"id": item["id"], "agent_id": rev["agent_id"]})

    assert out["status"] == "review", "released, not bounced — nothing is wrong with the work"
    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.review_claimed_by is None, "the hold is gone, so another reviewer can take it"
    assert row.built_by == worker["agent_id"], "and the AUTHOR is untouched by a reviewer's release"


# ---- authorship: claiming is not making ------------------------------------------------------

def test_declining_an_item_does_not_make_you_its_author(client, auth, proj, key, db):
    """GRPH-434. `built_by` is written at claim and never cleared — correct, because releasing a
    lease must not destroy the record of who made the thing (GRPH-376/377). But an agent that
    claimed, wrote nothing and handed it back made nothing, and the stamp then barred it from
    ever REVIEWING the item it declined."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    item = _ok(client, key, "create_item", {"title": "not for me", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.built_by == me["agent_id"], "the claim stamps it — that is the behaviour under test"

    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})

    db.refresh(row)
    assert row.built_by is None, "an item nobody worked has no author"
    assert row.status == "next" and row.claimed_by is None


def test_working_an_item_and_then_releasing_it_keeps_the_authorship(client, auth, proj, key, db):
    """The half that must not regress. GRPH-377 was exactly this: a lease released while work
    existed, and the authorship going with it — which made the self-review ban unprovable after
    the fact. One substantive write is the line."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    item = _ok(client, key, "create_item", {"title": "half done", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    _ok(client, key, "update_item", {"id": item["id"], "touchpoints": ["backend/app/x.py"],
                                     "agent_id": me["agent_id"]})

    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})

    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.built_by == me["agent_id"], "work happened here — the author survives the release"


def test_a_second_claimant_still_becomes_the_author(client, auth, proj, key, db):
    """Clearing on release must not leave a hole: the next agent to claim is the author, exactly
    as before."""
    a = _ok(client, key, "register_agent", {"label": "a"})
    b = _ok(client, key, "register_agent", {"label": "b"})
    item = _ok(client, key, "create_item", {"title": "passed along", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": a["agent_id"]})
    _ok(client, key, "release_item", {"id": item["id"], "agent_id": a["agent_id"]})

    _ok(client, key, "claim_next", {"agent_id": b["agent_id"]})

    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.built_by == b["agent_id"]


# ---- a reservation is work (GRPH-435) ------------------------------------------------------

def test_claiming_a_cluster_and_releasing_it_keeps_the_authorship(client, auth, proj, key, db):
    """`claim_cluster` records the work in a different place, and the guard could not see it.

    The rule the clock enforces is "nothing has been written since the claim". Area
    reservations ARE written — a row carrying this agent and this item, created by the act of
    taking it — and they do not touch `items.updated_at`. So the guard read an untouched item
    and cleared `built_by` on an agent that had demonstrably taken the work.

    This is the primary claim path, not a corner: GRPH-380 made the all-in-one posture claim
    through the divvy, and claim-a-cluster-then-decline is an ordinary move.
    """
    me = _ok(client, key, "register_agent", {"label": "w"})
    item = _ok(client, key, "create_item", {"title": "clustered", "status": "next",
                                            "touchpoints": ["backend/app/services/items.py"]})
    got = _ok(client, key, "claim_cluster", {"agent_id": me["agent_id"]})
    assert got["claimed"], got
    assert got.get("areas"), "no areas reserved — this test would prove nothing"

    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})

    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.built_by == me["agent_id"], (
        "the agent reserved areas for this item, which is work — the author must survive")


def test_that_agent_is_then_refused_by_sign_off(client, auth, proj, key, db):
    """The consequence, end to end, and the reason this is a defect rather than a wart.

    With `built_by` cleared, `independent()` has nothing to refuse: the agent that claimed
    the cluster, held its areas and released it could sign off that same item. The self-review
    ban is the gate the whole review model rests on, and this walked around it through the
    normal claim path.
    """
    me = _ok(client, key, "register_agent", {"label": "w"})
    item = _ok(client, key, "create_item", {"title": "clustered", "status": "next",
                                            "touchpoints": ["backend/app/services/items.py"]})
    _ok(client, key, "claim_cluster", {"agent_id": me["agent_id"]})
    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})
    _ok(client, key, "update_item", {"id": item["id"], "status": "review"})

    # Read RAW, not through `_ok`: a refusal comes back as an error result whose text is a
    # message rather than JSON, so the success helper cannot express this assertion.
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "sign_off",
                                                 "arguments": {"id": item["id"],
                                                               "agent_id": me["agent_id"]}}},
                    headers={"X-API-Key": key}).json()["result"]
    assert r.get("isError"), f"sign_off accepted the agent that took this cluster: {r}"
    text = r["content"][0]["text"].lower()
    assert "own" in text or "independent" in text or "review" in text, text


def test_claiming_and_writing_nothing_at_all_still_loses_it(client, auth, proj, key, db):
    """GRPH-434's fix must survive. An agent that claimed, reserved NOTHING and released
    wrote nothing anywhere, and barring it from ever reviewing what it declined is the cost
    that fix exists to remove."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    item = _ok(client, key, "create_item", {"title": "untouched", "status": "next"})
    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})

    row = db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()
    assert row.built_by is None, "nothing was written anywhere — the claim alone is not authorship"


def _reserve(db, agent_id, item_row, area="backend/app/services/other.py"):
    from datetime import datetime, timedelta, timezone

    from app.models import AreaReservation

    db.add(AreaReservation(
        agent_id=agent_id, item_id=item_row.id, area=area,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600), predicted=False,
    ))
    db.commit()


def _row(db, item):
    return db.query(Item).filter(Item.number == int(item["id"].split("-")[-1])).one()


def test_a_reservation_on_a_DIFFERENT_item_does_not_save_the_authorship(client, auth, proj,
                                                                       key, db):
    """The query must name the item, not just the agent.

    Both of the tests above use one agent and one item, so a lookup that asked only "does
    this agent hold ANY reservation" passed them perfectly — it survived the sabotage pass.
    An agent working on one item would then keep authorship on every unrelated item it
    claimed and abandoned, which is the GRPH-434 defect back again by a different route.
    """
    me = _ok(client, key, "register_agent", {"label": "w"})
    busy = _ok(client, key, "create_item", {"title": "elsewhere", "status": "backlog"})
    idle = _ok(client, key, "create_item", {"title": "untouched", "status": "next"})
    _reserve(db, me["agent_id"], _row(db, busy))

    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    _ok(client, key, "release_item", {"id": idle["id"], "agent_id": me["agent_id"]})

    db.expire_all()
    assert _row(db, idle).built_by is None, (
        "the reservation was for another item — it is not work on this one")


def test_ANOTHER_agents_reservation_does_not_save_your_authorship(client, auth, proj, key, db):
    """And it must name the agent. The same one-agent blind spot: a lookup that asked only
    "is this item reserved by anyone" also passed both tests above, and would let an agent
    keep authorship on the strength of somebody else's work."""
    me = _ok(client, key, "register_agent", {"label": "me"})
    other = _ok(client, key, "register_agent", {"label": "other"})
    item = _ok(client, key, "create_item", {"title": "untouched", "status": "next"})
    _reserve(db, other["agent_id"], _row(db, item))

    _ok(client, key, "claim_next", {"agent_id": me["agent_id"]})
    _ok(client, key, "release_item", {"id": item["id"], "agent_id": me["agent_id"]})

    db.expire_all()
    assert _row(db, item).built_by is None, (
        "somebody else reserved this — that is not evidence THIS agent did anything")
