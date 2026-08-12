"""D3 — claim_review and the self-review ban (GRPH-334 / PRD-17).

**The load-bearing invariant of the whole PRD: an agent cannot pass its own work.** Every
other rule follows from it. Today `review` is a status an item sits in — nothing routes it to
a *different* agent, and nothing stops the author marking their own work `done`. With more
than one agent in the room, self-review stops being a procedural discipline somebody remembers
and becomes a `WHERE claimed_by != :caller` clause.

**Accept:** the only agent in the fleet cannot review its own item — `claim_review` returns
`{claimed: false, reason: "no item awaiting a second pair of eyes"}`. With two agents, A's item
is reviewable by B and never by A. `Item.reviewed_by != claimed_by` holds for every `done`
item. A bounced item is invisible to other workers until its pin lapses.

The attack these tests exist for is the obvious one: **promote a worker to reviewer while it
holds its own item.** It must not work, and it does not, because an agent's id does not change
when its role does — so the ban is keyed on authorship rather than on role. Two independent
gates enforce that, and both are tested, because a single gate keyed on a QUERY is one refactor
away from being keyed on the caller's current role instead.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent, Item
from app.services import fleet
from app.services import items as items_svc


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


def _refused(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert res.get("isError") is True, res
    return res["structuredContent"]["error"]


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
    """A project of its own. The seeded prototype dataset ships items already in `review` with
    no `claimed_by`, and those are legitimately reviewable by anyone — so a fleet test run
    against `core` reviews the SEED instead of the work it just created. The vendor-preference
    test failed exactly that way before this existed."""
    return client.post("/api/projects", json={"name": "FleetReview"},
                       headers=auth).json()["id"]


@pytest.fixture()
def agent_key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _new_item(client, key, title="work"):
    return _ok(client, key, "create_item", {"title": title, "status": "next"})


def _built_by(client, key, agent, title="work"):
    """An item claimed and pushed to `review` by `agent` — the state a reviewer acts on."""
    _new_item(client, key, title)
    c = _ok(client, key, "claim_next", {"agent_id": agent["agent_id"]})
    assert c["claimed"], "the fixture item should have been claimable"
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": agent["agent_id"]})
    return c["item"]["id"]


def _register(client, key, role, label=None, vendor=None):
    caps = {"vendor": vendor} if vendor else {}
    return _ok(client, key, "register_agent",
               {"label": label or f"{role} term", "role_hint": role, "capabilities": caps})


# ---- the invariant ------------------------------------------------------------------------

def test_a_lone_agent_cannot_review_its_own_work(client, agent_key, db):
    """THE acceptance criterion, and the route to it is worth stating.

    A worker cannot call `claim_review` at all — the D2 role gate refuses it before authorship
    is ever consulted, which is a stronger guarantee than this criterion asks for. So the real
    single-agent case is the one where the agent is PROMOTED to reviewer while holding its own
    work, and then finds there is nothing it may take.

    Note the shape of the answer: `claimed: false` with a reason, not an error. With one agent
    that is the CORRECT outcome, and phrasing it as a failure would send a solo agent hunting
    for a bug that is not there.
    """
    me = _register(client, agent_key, "worker")
    _built_by(client, agent_key, me)

    db.get(Agent, me["agent_id"]).active_role = "reviewer"   # promoted, same identity
    db.commit()
    out = _ok(client, agent_key, "claim_review", {"agent_id": me["agent_id"]})

    assert out["claimed"] is False
    assert out["reason"] == "no item awaiting a second pair of eyes"


def test_two_agents_can_review_each_other(client, agent_key, db):
    """With a second pair of eyes in the room the item routes — the payoff for the ban."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    b = _register(client, agent_key, "reviewer", label="B")

    out = _ok(client, agent_key, "claim_review", {"agent_id": b["agent_id"]})

    assert out["claimed"] is True
    assert out["item"]["id"] == item_key
    assert out["worker_agent"] == a["agent_id"]


def test_a_promoted_worker_still_cannot_sign_off_its_own_item(client, agent_key, db):
    """THE attack on a dynamic-role system: promote yourself to reviewer while holding your
    own work. It fails because the ban is keyed on AUTHORSHIP — an agent's id does not change
    when its role does, so it is still that item's `claimed_by`."""
    me = _register(client, agent_key, "worker")
    item_key = _built_by(client, agent_key, me)

    agent = db.get(Agent, me["agent_id"])
    agent.active_role = "reviewer"      # the promotion
    db.commit()

    err = _refused(client, agent_key, "sign_off",
                   {"id": item_key, "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "cannot sign it off" in err["message"]


def test_sign_off_is_a_second_independent_gate(db, client, agent_key):
    """Redundant on the happy path, deliberately. `claim_review` already filters by
    authorship, so this assertion should never fire — but a single gate keyed on a QUERY is
    one refactor away from being keyed on the caller's current role, and that failure would be
    silent: work signing itself off while every role test still passed."""
    me = _register(client, agent_key, "worker")
    _new_item(client, agent_key)
    _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item = db.query(Item).filter(Item.claimed_by == me["agent_id"]).one()

    with pytest.raises(fleet.SelfReview):
        fleet.sign_off(db, item_id=item.id, agent_id=me["agent_id"])


def test_every_done_item_was_signed_by_someone_else(client, agent_key, db):
    """The invariant stated as a property over the data rather than over one call."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    b = _register(client, agent_key, "reviewer", label="B")
    _ok(client, agent_key, "claim_review", {"agent_id": b["agent_id"]})

    signed = _ok(client, agent_key, "sign_off",
                 {"id": item_key, "agent_id": b["agent_id"],
                  "evidence": [{"kind": "note", "detail": "read the diff"}]})

    assert signed["status"] == "done"
    row = db.query(Item).filter(Item.status == "done", Item.reviewed_by.isnot(None)).first()
    assert row.reviewed_by == b["agent_id"]
    assert row.reviewed_by != a["agent_id"]


# ---- vendor diversity -----------------------------------------------------------------------

def test_review_prefers_a_different_vendor(client, agent_key, db):
    """A Claude reviewer approving Claude work is a different agent but not a different error
    distribution — same training, same blind spots. This upgrades the invariant from
    preventing SELF-review to preventing MONOCULTURE review."""
    anthropic_worker = _register(client, agent_key, "worker", label="A", vendor="anthropic")
    openai_worker = _register(client, agent_key, "worker", label="B", vendor="openai")
    for who in (anthropic_worker, openai_worker):
        _built_by(client, agent_key, who)

    rev = _register(client, agent_key, "reviewer", label="R", vendor="anthropic")
    out = _ok(client, agent_key, "claim_review", {"agent_id": rev["agent_id"]})

    assert out["worker_agent"] == openai_worker["agent_id"], "crossed the vendor line"


def test_vendor_preference_never_blocks_a_review(client, agent_key):
    """A preference, not a requirement. A same-vendor review is far better than none, and a
    fleet that is all one vendor is the normal case rather than the exception."""
    a = _register(client, agent_key, "worker", label="A", vendor="anthropic")
    _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R", vendor="anthropic")

    out = _ok(client, agent_key, "claim_review", {"agent_id": rev["agent_id"]})

    assert out["claimed"] is True


# ---- bounce and the author pin --------------------------------------------------------------

def test_a_bounce_needs_a_reason(client, agent_key):
    """A bounce without one is a rejection the author cannot act on, and it costs them a full
    cycle to find that out."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")

    err = _refused(client, agent_key, "bounce",
                   {"id": item_key, "agent_id": rev["agent_id"], "reason": "   "})

    assert err["code"] == "validation"


def test_a_bounced_item_is_reserved_for_its_author(client, agent_key, db):
    """The author still has the worktree, the branch and the review comment in context.
    Handing that to a cold agent throws away exactly what cluster assignment preserves."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")
    _ok(client, agent_key, "bounce",
        {"id": item_key, "agent_id": rev["agent_id"], "reason": "tests missing"})

    other = _register(client, agent_key, "worker", label="C")
    stolen = _ok(client, agent_key, "claim_next", {"agent_id": other["agent_id"]})

    assert not stolen["claimed"] or stolen["item"]["id"] != item_key


def test_the_author_can_take_their_bounced_item_straight_back(client, agent_key):
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")
    _ok(client, agent_key, "bounce",
        {"id": item_key, "agent_id": rev["agent_id"], "reason": "tests missing"})

    again = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})

    assert again["claimed"] and again["item"]["id"] == item_key


def test_the_pin_lapses_so_an_item_is_never_stranded(client, agent_key, db):
    """A hard author-only pin is the tempting version and it is wrong: it strands the item
    when the author never comes back — re-tasked, or dead — which is the common case rather
    than the exotic one."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")
    _ok(client, agent_key, "bounce",
        {"id": item_key, "agent_id": rev["agent_id"], "reason": "tests missing"})

    row = db.query(Item).filter(Item.bounce_pinned_to == a["agent_id"]).one()
    row.bounce_pinned_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    other = _register(client, agent_key, "worker", label="C")
    taken = _ok(client, agent_key, "claim_next", {"agent_id": other["agent_id"]})

    assert taken["claimed"] and taken["item"]["id"] == item_key
