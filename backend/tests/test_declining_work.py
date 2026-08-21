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
