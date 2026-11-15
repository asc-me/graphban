"""GRPH-1652 — `claim_review` names what it withheld, by item and rule.

GBC-136 was built, bounced, re-delegated, rebuilt and moved to `review`. Three review-only
seats from the same planner then called `claim_review`, were never handed it, and were told
"no item awaiting a second pair of eyes". The item sat in `review` with nobody holding it.

The sequence alone does not withhold anything: a plain bounce-and-rebuild is handed out (the
first test). What does is the rebuild's SUPERVISOR: it launched the attempt and the rebuilder
exited without the branch being reported published, so `publish_pending` withholds the item
until a publish report, a passed `suite_green` on the item's branch, or the item leaving
review. That rule was missing from `review_block_reason`, so the answer said the queue was
empty. Both halves are pinned here.
"""
from __future__ import annotations

from app.models import AttemptTelemetry
from app.services import fleet as fleet_svc
from app.services import harness as hsvc
from tests.test_delegation import (  # noqa: F401 — fixtures
    _agent, _delegate, _mcp, _ok, _stored, db, key, proj)


def _seat(db, proj, planner, **kw) -> str:
    _, code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker",
                                        minted_by=planner, **kw)
    return code


def _rebuilt(client, key, proj, db, *, supervised: bool) -> tuple[str, str, str]:
    """Build, bounce, re-delegate to a new builder, rebuild, move to review."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _ok(_mcp(client, key, "create_item", {
        "title": "gbc-136", "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]

    first = _ok(_delegate(client, key, item, planner, seat=True))
    b1 = _agent(client, key, "builder-1", enrolment_code=first["enrolment_code"],
                branch="gb/one", capabilities={"vendor": "cursor"})
    assert _stored(db, item).claimed_by == b1
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": b1}))

    r1 = _agent(client, key, "reviewer-1", enrolment_code=_seat(db, proj, planner, review_only=True),
                capabilities={"vendor": "anthropic"})
    assert _ok(_mcp(client, key, "claim_review", {"agent_id": r1}))["claimed"]
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": r1, "reason": "missing test"}))

    stored = _stored(db, item)
    stored.bounce_pinned_until = None
    stored.bounce_pinned_to = None
    db.commit()
    second = _ok(_delegate(client, key, item, planner, tier="frontier", seat=True))
    b2 = _agent(client, key, "builder-2", enrolment_code=second["enrolment_code"],
                branch="gb/two", capabilities={"vendor": "anthropic"})
    assert _stored(db, item).claimed_by == b2
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": b2,
                                           "evidence": [{"kind": "note", "detail": "rebuilt"}]}))
    if supervised:
        # The re-delegation's launch post; the rebuilder exited before its branch was published.
        db.add(AttemptTelemetry(id=f"at_{item}", item_id=_stored(db, item).id,
                                project_id=proj, chosen_source="matrix",
                                chosen_winner="claude:opus", branch_published_at=None))
        db.commit()
    stored = _stored(db, item)
    assert stored.status == "review" and stored.built_by == b2
    assert stored.review_claimed_by is None
    return item, planner, b2


def test_a_rebuilt_item_is_handed_to_a_review_seat_from_the_same_planner(client, key, proj, db):
    """The sequence on its own withholds nothing. Sabotage: add `or delegated_by(db, it,
    <seat's minter>)` to the filter — the seat is refused and this fails."""
    item, planner, _ = _rebuilt(client, key, proj, db, supervised=False)
    r2 = _agent(client, key, "reviewer-2", enrolment_code=_seat(db, proj, planner, review_only=True),
                capabilities={"vendor": "cursor"})
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": r2}))
    assert got["claimed"] and got["item"]["id"] == item
    assert got["withheld"] == []


def test_a_withheld_rebuild_is_named_with_its_rule(client, key, proj, db):
    """The GBC-136 case. Sabotage: drop the `withheld` key from the MCP reply, or the
    `review_withheld` fallback in `review_block_reason` — the reviewer is told the queue is
    empty and this fails."""
    item, planner, b2 = _rebuilt(client, key, proj, db, supervised=True)
    for label in ("reviewer-2", "reviewer-3"):
        rev = _agent(client, key, label, enrolment_code=_seat(db, proj, planner, review_only=True),
                     capabilities={"vendor": "anthropic"})
        got = _ok(_mcp(client, key, "claim_review", {"agent_id": rev}))
        assert not got["claimed"]
        assert got["reason"] != "no item awaiting a second pair of eyes"
        assert item in got["reason"] and "publish_pending" in got["reason"]
        [w] = got["withheld"]
        assert w["item"] == item and w["rule"] == fleet_svc.WITHHELD_PUBLISH_PENDING
        assert "gb/two" in w["reason"] and "suite_green" in w["reason"]

    # The release the reason names: the supervisor reports the push.
    row = db.get(AttemptTelemetry, f"at_{item}")
    row.branch_published_at = hsvc.published_now()
    db.commit()
    rev = _agent(client, key, "reviewer-4", enrolment_code=_seat(db, proj, planner, review_only=True))
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": rev}))
    assert got["claimed"] and got["item"]["id"] == item, got["reason"]


def test_the_listing_and_the_filter_are_one_rule(client, key, proj, db):
    """Every in-review item is either handed out or listed, never neither. Own work and a
    held review are named too; a skipped item is the caller's own choice and is left out."""
    item, planner, b2 = _rebuilt(client, key, proj, db, supervised=False)
    mine = fleet_svc.review_withheld(db, agent_id=b2, project_id=proj)
    assert [(w["item"], w["rule"]) for w in mine] == [(item, fleet_svc.WITHHELD_OWN_WORK)]
    assert fleet_svc.review_withheld(db, agent_id=b2, project_id=proj, skip=[item]) == []

    holder = _agent(client, key, "holder", enrolment_code=_seat(db, proj, planner, review_only=True))
    assert fleet_svc.claim_review(db, agent_id=holder, project_id=proj).key == item
    late = _agent(client, key, "late", enrolment_code=_seat(db, proj, planner, review_only=True))
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": late}))
    assert not got["claimed"]
    assert [(w["item"], w["rule"]) for w in got["withheld"]] == [(item, fleet_svc.WITHHELD_HELD)]
    assert holder in got["withheld"][0]["reason"]
