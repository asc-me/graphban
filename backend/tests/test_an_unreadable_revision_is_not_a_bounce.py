"""An unreadable revision is not the builder's fault, and must not be charged to them (GRPH-991).

GRPH-987's fleet work publishes the branch when an item reaches review and tells a reviewer
NOT_YET when the revision it names cannot be fetched — a supervisor that has not pushed yet, a
branch name that never existed, a 404 in the window between the status move and the reap. But
that is a SENTENCE in a prompt, and the server advertised exactly one verb for handing review
work back: `bounce`. A reviewer that followed the manifest rather than the prose wrote
`outcome="bounced"` into the builder's cell of the preference matrix, so a publishing race in
the harness was charged to the vendor and model that built the work — and the matrix is what
decides who gets the next item.

The fix is not a new outcome inside `record_review_verdict`. `_recompute_check` treats every
verdict that is not `bounced` as a sign-off, so a third value there would certify an undecided
review as a confirmed one: the absence reading as clean, wearing the clothes of the thing that
removes it. It is the release verb that already existed and could not be found —
`release_item` on a review claim leaves the item in `review`, writes nothing about the builder,
and now carries the reason so the next reviewer is not left to rediscover it.

Each test names the acceptance criterion it pins and the sabotage it survives. Rows are read
through a session opened after the request, so what is inspected is what the app committed.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AttemptTelemetry, HarnessReviewCheck, HarnessRollup, Item
from app.services import harness as hsvc
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


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "NotYet"}, headers=auth).json()["id"]


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


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"branch": "gb/test", "label": label, **kw}))["agent_id"]


#: The builder's declared caps, so its cell in the matrix is identifiable rather than
#: whichever anonymous bucket an undeclared agent falls into.
BUILDER = {"vendor": "anthropic", "model": "sonnet", "tier": "cheap"}


def _in_review(client, key, db, planner, title="work"):
    """A delegated item, built by a declared child, moved to `review` — and the child still
    holds the build lease, which is the state a reviewer meets in the fleet."""
    item = _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]
    did = _ok(_mcp(client, key, "delegate",
                   {"id": item, "lane": "backend", "tier": "cheap",
                    "agent_id": planner}))["delegation_id"]
    child = _agent(client, key, f"child-{title}", parent_agent_id=planner,
                   capabilities={"instance": title, **BUILDER})
    assert items_svc.claim_item(db, item, child) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    return item, did, child


def _reviewer(client, key, proj, label="rev"):
    """A reviewer that has LEASED the item — the position a real reviewer is in when its fetch
    of the branch fails, and the only one from which `release_item` takes the review branch."""
    reviewer = _agent(client, key, f"reviewer-{label}",
                      capabilities={"instance": f"rev-{label}"})
    claimed = _ok(_mcp(client, key, "claim_review", {"project_id": proj, "agent_id": reviewer}))
    assert claimed.get("claimed"), claimed
    return reviewer


def _cell(db, proj) -> list[tuple]:
    """The builder's cells in the preference matrix, as the matrix actually reads them.

    Rolled rather than inspected at the source, because "the cell is unchanged" is a claim
    about the number a planner is shown — and `roll` is what turns raw attempts into it.
    """
    hsvc.roll(db, proj)
    return sorted((r.vendor, r.model, r.finished, r.signed_off, r.bounced)
                  for r in db.scalars(select(HarnessRollup)).all())


def _attempts(db) -> list[AttemptTelemetry]:
    db.expire_all()
    return list(db.scalars(select(AttemptTelemetry)).all())


# ---- acceptance clause 1: the cell does not move ------------------------------------------------


def test_an_unreadable_revision_leaves_the_builders_matrix_cell_unchanged(client, key, proj, db):
    """ACCEPTANCE. The reviewer could not read the revision, said so through the release verb,
    and the builder's cell is byte-for-byte what it was before the review started.

    Sabotage: route the release through `bounce` instead — or add the missing
    `delegation.on_outcome` call to the review branch of `release_item` — and the cell grows a
    bounce that the builder did not earn."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)

    before = _cell(db, proj)
    reviewer = _reviewer(client, key, proj)

    out = _ok(_mcp(client, key, "release_item", {
        "id": item, "agent_id": reviewer,
        "reason": "branch gb/work is not fetchable from the remote — revision unreadable"}))

    db.expire_all()
    assert _cell(db, proj) == before, "an unreadable revision was charged to the builder"
    # The mechanism, stated rather than implied: no attempt was closed, so there is no row for
    # `roll` to count. A cell that stayed the same because the rollup is stale would pass the
    # assertion above and still be a lie.
    assert _attempts(db) == [], "a not-yet must not close the builder's attempt"
    assert out["status"] == "review", "handing a review claim back is not sending it to `next`"


def test_a_bounce_for_the_same_reason_does_move_the_cell(client, key, proj, db):
    """THE CONTROL, and the reason the test above means anything. Same builder, same reviewer,
    same unreadable revision — the only difference is the verb. Without this arm, "the cell did
    not move" could pass because nothing in the fixture was ever capable of moving it.

    Sabotage: this is the sabotage. It is the pre-fix path, and it is what a reviewer following
    the manifest rather than the prose did."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)

    before = _cell(db, proj)
    reviewer = _reviewer(client, key, proj)
    _ok(_mcp(client, key, "bounce", {
        "id": item, "agent_id": reviewer,
        "reason": "branch gb/work is not fetchable from the remote — revision unreadable"}))

    db.expire_all()
    after = _cell(db, proj)
    assert after != before, "a bounce that moves nothing would make the not-yet test vacuous"
    rows = _attempts(db)
    assert len(rows) == 1 and rows[0].outcome == "bounced"
    assert (rows[0].vendor, rows[0].model) == (BUILDER["vendor"], BUILDER["model"]), \
        "and it is charged to the vendor and model that BUILT the work, which is the defect"


# ---- what the outcome is, as opposed to what it is not ----------------------------------------


def test_the_item_stays_in_review_and_the_next_reviewer_can_take_it(client, key, proj, db):
    """A not-yet releases the CLAIM, not the work. The item is still awaiting review and is
    immediately claimable by somebody else — which is the difference from a bounce, where it
    goes back to `next` and its author has to reclaim it.

    Sabotage: let the review branch fall through to the build-release path and the item lands
    in `next`, so this fails on the status and on the second reviewer's claim."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)
    first = _reviewer(client, key, proj, "first")

    _ok(_mcp(client, key, "release_item",
             {"id": item, "agent_id": first, "reason": "revision unreadable"}))

    db.expire_all()
    stored = db.get(Item, item)
    assert stored.status == "review"
    assert stored.review_claimed_by is None, "the claim is the thing being handed back"
    assert stored.built_by == child, "authorship is not erased by somebody else's release"

    second = _agent(client, key, "reviewer-second", capabilities={"instance": "rev-second"})
    assert _ok(_mcp(client, key, "claim_review",
                    {"project_id": proj, "agent_id": second})).get("claimed"), \
        "the point of releasing is that the next reviewer gets it"


def test_the_reason_is_kept_for_the_next_reviewer(client, key, proj, db):
    """The half that stops this being a silence. A release with no record is indistinguishable
    from a reviewer that never looked, and the next one repeats the same failed fetch — so the
    reason is appended to the item's evidence, which is the first thing a reviewer reads.

    Sabotage: drop the `_record_handback` call and this fails while every other test here still
    passes, which is exactly why it needs its own."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)
    reviewer = _reviewer(client, key, proj)

    _ok(_mcp(client, key, "release_item", {
        "id": item, "agent_id": reviewer,
        "reason": "branch gb/work is not fetchable from the remote"}))

    db.expire_all()
    notes = [e.get("detail", "") for e in (db.get(Item, item).evidence or [])
             if e.get("kind") == "note"]
    assert any(n.startswith(f"NOT YET ({reviewer}):") for n in notes), notes
    assert any("not fetchable" in n for n in notes), notes


def test_a_release_without_a_reason_still_releases(client, key, proj, db):
    """`reason` is optional and stays optional. A reviewer that correctly refused an item — its
    own work, say — has no reason to give, and requiring one would send it back to the only verb
    that does not ask: `bounce`, the punitive one. That is GRPH-429's case, and this change must
    not break it.

    Sabotage: make the reason required and this fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)
    reviewer = _reviewer(client, key, proj)

    out = _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": reviewer}))

    db.expire_all()
    stored = db.get(Item, item)
    assert out["status"] == "review"
    assert stored.review_claimed_by is None
    assert [e for e in (stored.evidence or []) if e.get("kind") == "note"] == [], \
        "nothing to say is not a note saying nothing"


def test_a_not_yet_writes_no_verdict_the_harness_would_certify(client, key, proj, db):
    """The trap the item's own handoff named. `record_review_verdict` accepts `signed_off` and
    `bounced`, and `_recompute_check` treats EVERY verdict that is not `bounced` as a sign-off
    and then confirms it once the review window closes. So the tempting implementation — a third
    verdict value — would have turned an undecided review into a confirmed one, and the matrix
    would have read it as the builder's work passing review.

    Sabotage: call `record_review_verdict(db, item, reviewer, "not_yet")` from the release path
    and a check row appears here."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)
    reviewer = _reviewer(client, key, proj)

    _ok(_mcp(client, key, "release_item",
             {"id": item, "agent_id": reviewer, "reason": "revision unreadable"}))

    db.expire_all()
    assert db.scalars(select(HarnessReviewCheck)).all() == [], \
        "an undecided review was recorded as a verdict"


def test_the_take_count_survives_a_not_yet(client, key, proj, db):
    """`review_takes` counts takes WITHOUT a verdict, and a not-yet is one of them. Clearing it
    would let a reviewer loop that releases and re-takes look like a queue nobody has ever
    touched — the defect GRPH-771 added the count to catch, arriving by a new door.

    Sabotage: reset `review_takes` in the release path and this fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _in_review(client, key, db, planner)
    reviewer = _reviewer(client, key, proj)

    _ok(_mcp(client, key, "release_item",
             {"id": item, "agent_id": reviewer, "reason": "revision unreadable"}))

    db.expire_all()
    assert db.get(Item, item).review_takes == 1


# ---- the discoverability half: a control nobody can find is a sentence --------------------------


def test_the_release_verb_declares_the_reason_it_now_keeps(client, key):
    """The manifest is the discovery surface, and a `reason` the schema does not declare is a
    reason a caller cannot send — the GRPH-515 shape, where a parameter existed server-side and
    no agent could pass what it could not see.

    Sabotage: drop the property from the tool's inputSchema and this fails while every behavioural
    test above still passes, because they call the handler directly."""
    from app.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "release_item")
    assert "reason" in tool["inputSchema"]["properties"]
    assert "reason" not in tool["inputSchema"].get("required", []), \
        "required would send a reviewer with nothing to say back to `bounce`"


def test_the_manifest_says_a_review_claim_does_not_go_to_next(client, key):
    """The clause that made the punitive verb the default was `release_item` describing itself as
    "moves it back to `next` by default" — true of a build claim, false of a review one. A
    reviewer with an unfetchable branch read that, concluded the verb was for builders, and
    bounced. The description has to say the review case or the control is still a sentence.

    Sabotage: restore the old description and this fails."""
    from app.mcp_server import TOOLS

    text = next(t for t in TOOLS if t["name"] == "release_item")["description"]
    assert "review" in text.lower() and "next" in text.lower(), text
    assert "bounce" in text.lower(), "it has to point away from the punitive verb by name"
