"""A seat may be minted REVIEW-ONLY, and then it cannot take build work (GRPH-988).

Reported from two waves running side by side on 2026-09-29: builders on one adapter,
reviewers on another in a separate checkout, the reviewer supervisor started with
`--max-workers 0` so that it would build nothing. It built a slice anyway. That flag does pin
the supervisor's own `need` at zero and its delegation branch cannot fire — but the branch
that spawns for unheld review rows minted its child a plain WORKER seat, because `reviewer`
merged into `worker` in S3 (PRD-39) and a worker seat can `claim_next`. So the "reviewer"
claimed GRPH-965 and built it while the child actually assigned to that item produced nothing,
and a sign-off that was supposed to be cross-vendor came from the same vendor and model that
built the work.

The supervisor was already telling the truth about itself. What was missing is a seat the
SERVER will not let claim — the child's prompt is not a control, and authority in this codebase
comes from the credential.

Three things every test here is really checking, the first two inherited from the scope gate
this one is modelled on (GRPH-827):

- the gate is on the two writes that hand an item to a holder (`claim_item`, `claim_next`), so
  the paths above them inherit it rather than each restating it;
- a refusal and an empty result stay DIFFERENT answers, because "nothing ready" means stop
  asking and "you may not take any" means the seat is the thing to look at;
- and the seat can still do everything a review needs, or the fix is a broken wave wearing the
  clothes of a safe one.
"""
from __future__ import annotations

import pytest

from app.models import Enrolment, Item
from app.services import fleet as fleet_svc
from app.services import items as items_svc


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


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "ReviewOnly"}, headers=auth).json()["id"]


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


def _item(client, key, title, touchpoints, status="next"):
    return _ok(_mcp(client, key, "create_item",
                    {"title": title, "status": status, "touchpoints": touchpoints}))["id"]


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "branch": "gb/test", **kw}))["agent_id"]


@pytest.fixture()
def planner(client, auth, key, proj, db):
    """A planner that may mint. Seats are minted by a planner credential, so the fixture
    registers on one issued for the project rather than promoting a worker."""
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _agent(client, key, "planner", enrolment_code=code)


def _seat(client, key, planner, *, review_only: bool) -> str:
    """A child registered on a seat minted with `review_only` — the credential under test."""
    minted = _ok(_mcp(client, key, "mint_enrolment",
                      {"agent_id": planner, "role": "worker", "wave": "w",
                       "review_only": review_only}))
    return _agent(client, key, "child", enrolment_code=minted["enrolment_code"],
                  capabilities={"instance": "reviewer"}), minted


def _review_only(client, key, planner) -> str:
    return _seat(client, key, planner, review_only=True)[0]


def _plain(client, key, planner) -> str:
    return _seat(client, key, planner, review_only=False)[0]


def _in_review_by_somebody_else(client, key, db) -> str:
    """An item another agent built and moved to review, so `claim_review` can offer it."""
    who = _agent(client, key, "builder", worktree="/w/one", branch="gb/wave-1",
                 capabilities={"instance": "builder"})
    item = _item(client, key, "the work under review", ["backend/app/x.py"])
    assert items_svc.claim_item(db, item, who) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": who}))
    return item


def _stored(db, item_id) -> Item:
    db.expire_all()
    return db.get(Item, item_id)


# ---- the reported escape -------------------------------------------------------------------

def test_a_review_only_seat_cannot_claim_a_ready_item(client, auth, key, db, proj, planner):
    """THE ONE THAT MATTERS. The reported wave, in miniature: a supervisor that builds nothing
    has a child on the board, and there is ready unclaimed work in front of it.

    Sabotage: mint the child a plain worker seat (drop `review_only=True` in `until.py`'s
    review branch, or drop the gate in `claim_next`) and this claims the item."""
    ready = _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    with pytest.raises(items_svc.ReviewOnlySeat):
        items_svc.claim_next(db, child, project_id=proj)

    row = _stored(db, ready)
    assert row.claimed_by is None, row.claimed_by
    assert row.status == "next", row.status


def test_naming_an_item_is_refused_rather_than_reported_as_a_lost_race(
        client, auth, key, db, proj, planner):
    """`claim_item` returns None for "somebody beat you to it", and the right response to that
    is to try the next candidate. A seat that may not build has no next candidate, so it gets
    the exception — the flattening `OutOfScope` exists to avoid, avoided."""
    ready = _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    with pytest.raises(items_svc.ReviewOnlySeat):
        items_svc.claim_item(db, ready, child)

    assert _stored(db, ready).claimed_by is None


def test_claim_cluster_says_review_only_rather_than_nothing_ready(
        client, auth, key, db, proj, planner):
    """"Nothing ready to claim" is what an agent hears when the project is empty, and it means
    STOP. A reviewer child hearing that next to a board full of work would report the fleet as
    drained — which is the absence that reads as a clean result."""
    _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)

    assert got["claimed"] is False, got
    assert got["items"] == []
    assert got["reason"] != "nothing ready to claim"
    assert "review-only" in got["reason"]
    # The remedy is named, because a refusal that does not say what to do instead is a refusal
    # the child answers by retrying.
    assert "claim_review" in got["reason"]


def test_next_cluster_refuses_instead_of_returning_an_empty_batch(
        client, auth, key, db, proj, planner):
    """`next_cluster`'s seed comes through `claim_next`, so it inherits the gate — and its
    documented "empty list when nothing is ready" must NOT become the review-only answer.
    `claimed: 0, cluster: []` over a full board is the same false clean result."""
    from app.services import clustering as cluster_svc

    _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    with pytest.raises(items_svc.ReviewOnlySeat):
        cluster_svc.next_cluster(db, child, project_id=proj)


# ---- the refusal has to survive the wire ---------------------------------------------------

def test_the_refusal_reaches_the_agent_as_a_conflict_not_an_empty_board(
        client, auth, key, db, proj, planner):
    """THE CALL, not only the callee. Every test above drives the service directly; this one
    drives the surface a child actually uses, because a gate the dispatcher swallows into
    `internal` arrives as "safe to retry once" and a child will spend its whole turn budget
    believing that.

    Sabotage: delete the `except items_svc.ReviewOnlySeat` arm in `mcp_server.py` and the code
    becomes `internal`, not `conflict`."""
    _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    err = _err(_mcp(client, key, "claim_next", {"agent_id": child}))

    assert err["code"] == "conflict", err
    assert "review-only" in err["message"], err
    assert err.get("hint") and "claim_review" in err["hint"], err


def test_claim_cluster_over_the_wire_carries_the_reason(client, auth, key, db, proj, planner):
    """Same surface check for the tool a fleet child is actually told to call. This one is a
    structured refusal rather than an error, so the reason has to be IN the payload — an agent
    that only reads `claimed` sees a false "nothing to do"."""
    _item(client, key, "ready build work", ["svc/thing.py"])
    child = _review_only(client, key, planner)

    got = _ok(_mcp(client, key, "claim_cluster", {"agent_id": child}))

    assert got["claimed"] is False, got
    assert "review-only" in (got.get("reason") or ""), got


# ---- and the seat can still review ---------------------------------------------------------

def test_a_review_only_seat_can_still_claim_review(client, auth, key, db, proj, planner):
    """The control that decides whether this can ship. Removing `claim_next` from a reviewer
    is only a fix if review survives; a seat that can do nothing is a broken wave, not a safe
    one, and it would show up as a wave that never signs anything off."""
    item = _in_review_by_somebody_else(client, key, db)
    child = _review_only(client, key, planner)

    got = fleet_svc.claim_review(db, agent_id=child, project_id=proj)

    assert got is not None and got.id == item, got
    assert _stored(db, item).review_claimed_by == child


def test_a_review_only_child_is_told_what_its_seat_is(client, auth, key, db, proj, planner):
    """`register_agent` states the seat's kind, so a child learns it from the authority rather
    than by walking into a refusal — and so a client can tell "not review-only" from "this
    server has never heard of the flag", which is the distinction `until.py`'s probe reads.

    Asserted on BOTH sides: a field that only appears when true is indistinguishable from a
    server too old to send it, which is the exact ambiguity the probe exists to close."""
    minted = _ok(_mcp(client, key, "mint_enrolment",
                      {"agent_id": planner, "role": "worker", "wave": "w",
                       "review_only": True}))
    assert minted["review_only"] is True, minted
    reply = _ok(_mcp(client, key, "register_agent",
                     {"label": "child", "branch": "gb/test",
                      "enrolment_code": minted["enrolment_code"]}))
    assert reply["review_only"] is True, reply
    assert items_svc.review_only_seat(db, reply["agent_id"]) is True

    plain = _ok(_mcp(client, key, "mint_enrolment",
                    {"agent_id": planner, "role": "worker", "wave": "w"}))
    other = _ok(_mcp(client, key, "register_agent",
                     {"label": "builder", "branch": "gb/test",
                      "enrolment_code": plain["enrolment_code"]}))
    assert other["review_only"] is False, other


def test_a_plain_worker_seat_still_claims(client, auth, key, db, proj, planner):
    """The other control, and the one that decides whether this can ship next to a fleet that
    is already running: `review_only` defaults False, so every seat minted before the column
    existed — and every builder seat minted after it — keeps claiming exactly as it did."""
    ready = _item(client, key, "ready build work", ["svc/thing.py"])
    child = _plain(client, key, planner)

    got = items_svc.claim_next(db, child, project_id=proj)

    assert got is not None and got.id == ready, got
    assert items_svc.review_only_seat(db, child) is False


def test_an_agent_with_no_seat_is_unaffected(client, auth, key, db, proj, planner):
    """A person's own agent registers on a plain project key and holds no enrolment at all.
    The gate is keyed on the seat, never on the `worker` role, or it would stop somebody doing
    ops work through their own tools — the mistake `held_by_a_seat` exists to not repeat."""
    ready = _item(client, key, "ready build work", ["svc/thing.py"])
    person = _agent(client, key, "a person", capabilities={"instance": "person"})

    got = items_svc.claim_next(db, person, project_id=proj)

    assert got is not None and got.id == ready, got


# ---- the seat itself -----------------------------------------------------------------------

def test_a_review_only_seat_cannot_also_be_bound(client, auth, key, db, proj, planner):
    """A bound seat's item is BUILD-claimed at registration, which would make the reviewer the
    author of the one item it was steered to — and the self-review ban is keyed on authorship,
    so the seat would be refused the sign-off it was minted for. Contradiction refused at
    mint, where it is a typing mistake, rather than at registration, where it is a dead child."""
    item = _item(client, key, "some work", ["svc/thing.py"])

    with pytest.raises(ValueError, match="review-only"):
        fleet_svc.issue_enrolment(db, project_id=proj, role="worker", item_id=item,
                                  review_only=True)


def test_reissue_carries_review_only_across(client, auth, key, db, proj, planner):
    """The recovery path must not silently WIDEN. A review-only seat reissued as a plain worker
    seat hands the replacement child the `claim_next` its wave was forbidden, and nothing in
    the reissue reply would say so — a control a crash removes is not a control."""
    row, _ = fleet_svc.issue_enrolment(db, project_id=proj, role="worker", review_only=True)

    new, _ = fleet_svc.reissue_enrolment(db, enrolment_id=row.id)

    assert new.review_only is True
    assert new.reissued_from == row.id


def test_review_only_is_stored_on_the_seat_and_not_inferred(client, auth, key, db, proj):
    """The column, read back off the row. A flag that lived only in the mint reply would be a
    promise; the gate reads the enrolment, so the enrolment has to carry it."""
    row, _ = fleet_svc.issue_enrolment(db, project_id=proj, role="worker", review_only=True)
    default, _ = fleet_svc.issue_enrolment(db, project_id=proj, role="worker")

    db.expire_all()
    assert db.get(Enrolment, row.id).review_only is True
    assert db.get(Enrolment, default.id).review_only is False


def test_the_manifest_declares_review_only_so_the_fleet_can_probe_it():
    """The wire contract `until.check_review_only_is_honoured` reads. An MCP server that has
    never heard of an argument does not refuse it — `_validate_args` ignores unknown extras —
    so a supervisor against an older server would mint plain worker seats and report a wave
    that builds nothing while it builds.

    Read off `TOOLS` rather than a `tools/list` round trip: the served manifest is trimmed per
    credential and `mint_enrolment` is planner-only, so a project key never sees it. Trimming
    removes whole tools and does not rewrite their properties, so this is the same list the
    probe reads on the planner client it runs on.
    """
    from app.mcp_server import TOOLS

    mint = next(t for t in TOOLS if t["name"] == "mint_enrolment")

    assert "review_only" in mint["inputSchema"]["properties"], mint["inputSchema"]
