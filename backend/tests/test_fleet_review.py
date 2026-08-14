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


def _new_item(client, key, title="work", touchpoints=None):
    body = {"title": title, "status": "next"}
    if touchpoints:
        body["touchpoints"] = touchpoints
    return _ok(client, key, "create_item", body)


def _built_by(client, key, agent, title="work"):
    """An item claimed and pushed to `review` by `agent` — the state a reviewer acts on."""
    _new_item(client, key, title)
    c = _ok(client, key, "claim_next", {"agent_id": agent["agent_id"]})
    assert c["claimed"], "the fixture item should have been claimable"
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": agent["agent_id"]})
    return c["item"]["id"]


def _register(client, key, role, label=None, vendor=None, instance=None):
    """These share ONE credential, so each declares a distinct `instance`.

    That is the requirement rather than a convenience: on a shared key an agent must show
    something that differs to be independent, and absence is deliberately not a difference —
    otherwise omitting a field would launder a self-review. The label is already unique per
    call, so it doubles as the tag.
    """
    label = label or f"{role} term"
    caps = {"instance": instance or label}
    if vendor:
        caps["vendor"] = vendor
    return _ok(client, key, "register_agent",
               {"label": label, "role_hint": role, "capabilities": caps})


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
    # Submitted first, or `NotInReview` answers before the authorship gate is ever reached and
    # this stops testing what it names (GRPH-383).
    _ok(client, agent_key, "update_item",
        {"id": item.key, "status": "review", "agent_id": me["agent_id"]})
    db.refresh(item)

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


def test_the_bounce_reason_reaches_the_author(client, agent_key):
    """GRPH-378, found on the walk. `bounce` has always REQUIRED a reason and then dropped it:
    no column held it, the event meta carried only the principal, and on the live fleet DB the
    string appeared in no row of any table after a real bounce.

    So the author got the item back with nothing to act on — the exact failure the requirement
    exists to prevent. `test_a_bounce_needs_a_reason` above asserts the refusal on a blank
    reason and stops there, which is why 1768 green tests never noticed the reason went
    nowhere.

    Asserted on `get_item_details` because that is the read an author makes after reclaiming,
    and it builds its own dict — the divergence that hid the intent hold from the same read."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")

    _ok(client, agent_key, "bounce", {"id": item_key, "agent_id": rev["agent_id"],
                                      "reason": "no test covers the refusal path"})

    again = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})
    assert again["item"]["bounce_reason"] == "no test covers the refusal path"
    details = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert details["bounce_reason"] == "no test covers the refusal path", \
        "the read an author makes after reclaiming is the one that must carry it"


def test_a_refused_claim_is_distinguishable_from_an_empty_backlog(client, agent_key):
    """GRPH-379. `{"claimed": false, "item": null}` was byte-identical whether the backlog was
    empty or every ready item was pinned to somebody else — and those call for opposite
    behaviour: wait and retry, or stop asking.

    The pinned item is NOT named as reserved to its own author, who can simply take it."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)
    rev = _register(client, agent_key, "reviewer", label="R")
    _ok(client, agent_key, "bounce", {"id": item_key, "agent_id": rev["agent_id"],
                                      "reason": "tests missing"})
    other = _register(client, agent_key, "worker", label="C")

    refused = _ok(client, agent_key, "claim_next", {"agent_id": other["agent_id"]})

    assert refused["claimed"] is False
    assert refused["reserved"] == [{"id": item_key, "reserved_for": a["agent_id"],
                                    "reserved_until": refused["reserved"][0]["reserved_until"]}]
    assert refused["reserved"][0]["reserved_until"], "an agent deciding whether to wait needs the clock"


def test_an_empty_backlog_says_nothing_about_reservations(client, agent_key):
    """The other half of the distinction: when there is genuinely no work, the response must
    stay empty-looking. A `reserved` key that is always present would restore the ambiguity in
    the opposite direction — an agent would parse a field that never tells it anything.

    This replaced a test that asserted the author is not told about ITS OWN pin. That state is
    unreachable: a ready item pinned to the caller is one the caller just claims, so the
    branch never runs. Sabotaging the guard left it green, which is the tell — the assertion
    was vouching for a filter it could not exercise."""
    a = _register(client, agent_key, "worker", label="A")

    nothing = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})

    assert nothing["claimed"] is False
    assert "reserved" not in nothing


def test_authorship_is_readable_before_a_review(client, agent_key):
    """`built_by` decides review independence and was readable on no surface, so an agent could
    not tell whose work it was about to take, nor explain a refusal it received."""
    a = _register(client, agent_key, "worker", label="A")
    item_key = _built_by(client, agent_key, a)

    details = _ok(client, agent_key, "get_item_details", {"id": item_key})

    assert details["built_by"] == a["agent_id"]


# ---- the pin has to hold on EVERY claim path -------------------------------------------------

def _bounced(client, key, author, reviewer, title="pinned work"):
    """An item bounced back to `author`, so its pin is live."""
    item_key = _built_by(client, key, author, title=title)
    _ok(client, key, "bounce", {"id": item_key, "agent_id": reviewer["agent_id"],
                                "reason": "tests missing"})
    return item_key


def test_claim_cluster_cannot_take_a_pinned_item(client, agent_key):
    """The bypass, found while checking GRPH-380 and verified on the live fleet: an item pinned
    with 592 SECONDS REMAINING was taken by another agent through `claim_cluster`.

    `claim_item` — the path `claim_cluster` and `next_cluster` both claim through — never
    consulted the pin; only `claim_next` did. A reservation that holds on one of three paths is
    not a reservation, and GRPH-380 makes the broken path the DEFAULT posture's path."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    item_key = _bounced(client, agent_key, a, rev)
    other = _register(client, agent_key, "worker", label="C")

    out = _ok(client, agent_key, "claim_cluster", {"agent_id": other["agent_id"]})

    assert item_key not in [i["id"] for i in out.get("items", [])], \
        "claim_cluster took an item reserved for its author"


def test_next_cluster_cannot_take_a_pinned_item(client, agent_key):
    """The same hole through the third door. `next_cluster` seeds itself with `claim_next` — so
    the SEED respects the pin — and then claims NEIGHBOURS with `claim_item`, which did not.

    The pinned item therefore has to be a neighbour of the seed to be reachable at all: shared
    touchpoints are what make it one. A first draft of this test used an unrelated item, and it
    passed with the guard deleted — the claim path it was supposed to cover was never entered."""
    area = ["backend/app/services/shared_area.py"]
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    _new_item(client, agent_key, "pinned neighbour", touchpoints=area)
    c = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})
    item_key = c["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": a["agent_id"]})
    _ok(client, agent_key, "bounce", {"id": item_key, "agent_id": rev["agent_id"],
                                      "reason": "tests missing"})
    _new_item(client, agent_key, "the seed", touchpoints=area)
    other = _register(client, agent_key, "worker", label="C")

    out = _ok(client, agent_key, "next_cluster", {"agent_id": other["agent_id"]})

    claimed = [i["id"] for i in out["cluster"]]
    assert claimed, "the seed should have been claimable — otherwise this proves nothing"
    assert item_key not in claimed


def test_the_author_still_gets_its_pinned_item_from_claim_cluster(client, agent_key):
    """Refusing everyone would be a different bug: the pin exists to give the AUTHOR first
    refusal, so the author's own claim must go through on every path too."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    item_key = _bounced(client, agent_key, a, rev)

    out = _ok(client, agent_key, "claim_cluster", {"agent_id": a["agent_id"]})

    assert item_key in [i["id"] for i in out.get("items", [])]


def test_claiming_spends_the_reservation(client, agent_key, db):
    """A pin that outlives its claim reads as current and is not. `built_by` moves to whoever
    claims, while `bounce_pinned_to` kept naming the old author — so the item detail rendered a
    live reservation for an agent that does not hold the item, which is a wrong answer to the
    only question that field answers."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    item_key = _bounced(client, agent_key, a, rev)

    _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})

    details = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert "reserved_for" not in details, "the reservation is spent once somebody holds the item"
    assert details["bounce_reason"] == "tests missing", "the REASON survives the claim"


def test_a_collided_cluster_names_who_holds_the_areas(client, agent_key):
    """`all ready clusters collide with in-flight work` is unactionable to the caller most
    likely to see it — a solo human whose previous agent died holding areas, for whom the
    answer is either "wait N seconds" or "that agent is gone"."""
    first = _register(client, agent_key, "worker", label="first")
    _new_item(client, agent_key, "shared work", touchpoints=["backend/app/services/x.py"])
    taken = _ok(client, agent_key, "claim_cluster", {"agent_id": first["agent_id"]})
    assert taken["claimed"], "the fixture cluster should have been claimable"
    second = _register(client, agent_key, "worker", label="second")

    out = _ok(client, agent_key, "claim_cluster", {"agent_id": second["agent_id"]})

    assert out["claimed"] is False
    assert out["held_by"] == [first["agent_id"]]
    assert first["agent_id"] in out["reason"] and "frees in" in out["reason"]


# ---- danger mode: self-review, and everything it still refuses (GRPH-380) ---------------------

def _aio(client, key, label="solo"):
    """An ALL-IN-ONE agent: no role hint on an unnarrowed credential, which is what the default
    posture actually is. Registering it as a `worker` instead would meet the ROLE gate first —
    `sign_off requires role reviewer` — and never reach the self-review question at all."""
    return _ok(client, key, "register_agent",
               {"label": label, "capabilities": {"instance": label}})


def _built_by_aio(client, key, agent, title="work", effort=0):
    body = {"title": title, "status": "next"}
    if effort:
        body["effort"] = effort
    item = _ok(client, key, "create_item", body)
    c = _ok(client, key, "claim_next", {"agent_id": agent["agent_id"]})
    assert c["claimed"], "the fixture item should have been claimable"
    _ok(client, key, "update_item",
        {"id": item["id"], "status": "review", "agent_id": agent["agent_id"]})
    return item["id"]


def _danger(db, proj, on=True):
    from app.models import Project
    p = db.get(Project, proj)
    p.allow_self_review = on
    db.commit()


def test_a_solo_agent_is_stuck_without_danger_mode(client, agent_key, proj, db):
    """The configuration danger mode exists for, asserted BEFORE the escape hatch so the hatch
    is answering a real problem. An all-in-one agent now files into the review pool like every
    other posture — so a solo one finds only its own work there, and the gate refuses it.

    The refusal has to say that nobody else can review it either, or a solo operator reads
    "another agent has to take it" as advice and waits for an agent that is never coming."""
    a = _aio(client, agent_key)
    item_key = _built_by_aio(client, agent_key, a)

    err = _refused(client, agent_key, "sign_off",
                   {"id": item_key, "agent_id": a["agent_id"]})

    assert "no other agent here can review it" in err["message"]


def test_danger_mode_lets_a_solo_agent_sign_off_its_own_work(client, agent_key, proj, db):
    a = _aio(client, agent_key)
    item_key = _built_by_aio(client, agent_key, a)
    _danger(db, proj)

    out = _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert out["status"] == "done"
    assert out["built_by"] == a["agent_id"]


def test_danger_mode_still_refuses_while_another_agent_could_review(client, agent_key, proj, db):
    """THE load-bearing condition. An escape hatch usable while a reviewer is sitting there is
    not an escape hatch — it is the review gate switched off for everyone, and the flag would
    then mean "no review on this project" rather than "no review was possible"."""
    a = _aio(client, agent_key, label="A")
    _register(client, agent_key, "reviewer", label="R")
    item_key = _built_by_aio(client, agent_key, a, title="A's work")
    _danger(db, proj)

    err = _refused(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert err["code"] == "unauthorized"


def test_a_dead_reviewer_does_not_hold_the_gate_open(client, agent_key, proj, db):
    """The other half: an agent that cannot act must not count as one who could review. A
    reviewer that went offline would otherwise keep a solo agent blocked forever, which is the
    stall danger mode exists to end — arriving through presence instead of the flag."""
    from app.models import Agent
    from datetime import datetime, timedelta, timezone

    a = _aio(client, agent_key, label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    item_key = _built_by_aio(client, agent_key, a, title="A's work")
    _danger(db, proj)
    dead = db.get(Agent, rev["agent_id"])
    dead.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()

    out = _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert out["status"] == "done"


def test_a_self_review_says_so_on_the_item(client, agent_key, proj, db):
    """The bargain of danger mode is that it is VISIBLE. A self-review that leaves no trace is
    indistinguishable from a reviewed item on every surface a human reads."""
    a = _aio(client, agent_key)
    item_key = _built_by_aio(client, agent_key, a)
    _danger(db, proj)

    _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    details = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert details["built_by"] == details["reviewed_by"], "the row itself carries the fact"
    said = " ".join(e["detail"] for e in _ok(client, agent_key, "search_items",
                                             {"query": "work", "fields": "full"})["results"]
                    [0].get("evidence", []))
    assert "danger mode" in said


def test_danger_mode_does_not_relax_adversarial_evidence(client, agent_key, proj, db):
    """It relaxes INDEPENDENCE and nothing else. An effort-5 item signed off by its own author
    with no sabotage receipt would be the weakest possible review passing the strongest gate."""
    a = _aio(client, agent_key)
    item_key = _built_by_aio(client, agent_key, a, title="big", effort=5)
    _danger(db, proj)

    err = _refused(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert "adversarial evidence" in err["message"]


def test_a_worker_present_does_not_block_a_solo_reviewer(client, agent_key, proj, db):
    """The eligibility half of `could_review`: a WORKER cannot call `claim_review`, so its
    presence is not a reviewer's presence. Counting it would leave danger mode refusing on a
    project where nothing can ever review — the exact stall the mode exists to end.

    Written because sabotaging the role filter left the suite green: the earlier tests each had
    a single agent, so the filter was never reached."""
    a = _aio(client, agent_key, label="A")
    _register(client, agent_key, "worker", label="W")
    item_key = _built_by_aio(client, agent_key, a, title="A's work")
    _danger(db, proj)

    out = _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert out["status"] == "done"


def test_a_planner_present_does_not_block_it_either(client, agent_key, proj, db):
    """Same reasoning, the other role that cannot review. A planner deliberately holds no
    review tools — it is the one role with no authored work to launder."""
    a = _aio(client, agent_key, label="A")
    _register(client, agent_key, "planner", label="P")
    item_key = _built_by_aio(client, agent_key, a, title="A's work")
    _danger(db, proj)

    out = _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert out["status"] == "done"


def test_a_second_all_in_one_agent_does_block_it(client, agent_key, proj, db):
    """And the case that must still refuse. Two all-in-one agents are a fleet that reviews
    itself — that is the whole shape this ticket chose — so self-review is not the only option
    and danger mode does not apply."""
    a = _aio(client, agent_key, label="A")
    _aio(client, agent_key, label="B")
    item_key = _built_by_aio(client, agent_key, a, title="A's work")
    _danger(db, proj)

    err = _refused(client, agent_key, "sign_off", {"id": item_key, "agent_id": a["agent_id"]})

    assert err["code"] == "unauthorized"


# ---- a review verdict needs something submitted to review (GRPH-383) -------------------------

def test_sign_off_refuses_work_that_was_never_submitted(client, agent_key):
    """Found by using the fleet, not by a test. `FA-18`, `in_progress` and LEASED to one agent,
    went straight to `done` when another signed it off — every gate that existed passed,
    because the two agents were genuinely independent. The gates asked who, never whether.

    A verdict on unsubmitted work ends somebody else's lease mid-change and records a decision
    about a diff nobody was ever shown."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    _new_item(client, agent_key, "still being worked on")
    c = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})
    assert c["item"]["status"] == "in_progress"

    err = _refused(client, agent_key, "sign_off",
                   {"id": c["item"]["id"], "agent_id": rev["agent_id"]})

    assert err["code"] == "conflict", "the caller is permitted; the work is not submitted"
    assert a["agent_id"] in err["message"], "name who is still holding it"
    still = _ok(client, agent_key, "get_item_details", {"id": c["item"]["id"]})
    assert still["status"] == "in_progress" and still["claimed_by"] == a["agent_id"]


def test_bounce_refuses_work_that_was_never_submitted(client, agent_key):
    """Same hole, the other verdict — and worse in one way: a bounce would reset an item
    somebody is actively working to `next` and drop their lease."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    _new_item(client, agent_key, "mid-flight")
    c = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})

    err = _refused(client, agent_key, "bounce",
                   {"id": c["item"]["id"], "agent_id": rev["agent_id"], "reason": "no"})

    assert err["code"] == "conflict"
    still = _ok(client, agent_key, "get_item_details", {"id": c["item"]["id"]})
    assert still["status"] == "in_progress"


def test_a_done_item_cannot_be_signed_off_twice(client, agent_key):
    """The other side of the same check. Signing off a `done` item re-stamps `reviewed_by`,
    quietly reassigning credit for a review that already happened."""
    a = _register(client, agent_key, "worker", label="A")
    r1 = _register(client, agent_key, "reviewer", label="R1")
    r2 = _register(client, agent_key, "reviewer", label="R2")
    item_key = _built_by(client, agent_key, a)
    _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": r1["agent_id"]})

    err = _refused(client, agent_key, "sign_off", {"id": item_key, "agent_id": r2["agent_id"]})

    assert err["code"] == "conflict"
    assert _ok(client, agent_key, "get_item_details",
               {"id": item_key})["reviewed_by"] == r1["agent_id"]


def test_the_full_record_carries_its_evidence(client, agent_key):
    """`get_item_details` calls itself the full record and omitted `evidence` — so an agent
    could not read what a completion was justified by. The danger-mode self-review note lives
    there too, and a receipt nobody can read is not a disclosure."""
    a = _register(client, agent_key, "worker", label="A")
    rev = _register(client, agent_key, "reviewer", label="R")
    item_key = _built_by(client, agent_key, a)
    _ok(client, agent_key, "sign_off", {"id": item_key, "agent_id": rev["agent_id"],
                                        "evidence": [{"kind": "test", "detail": "42 passed"}]})

    details = _ok(client, agent_key, "get_item_details", {"id": item_key})

    assert [e["detail"] for e in details["evidence"]] == ["42 passed"]
