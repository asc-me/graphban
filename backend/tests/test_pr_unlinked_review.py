"""GRPH-1730 — claim_review does not hand out a review row whose PR is not linked yet.

A second ready row used to be irrelevant. claim_review orders by sort_order, so the
unlinked row (created first) was the one the reviewer received. Holding it hid the row
from the supervisor, and the PR open was never retried. The row stays withheld
(`pr_unlinked`) until a PR url is recorded. A different done-refusal, and an owner
close, are still handed out: those are verdicts a reviewer can clear.
"""
from __future__ import annotations

from app.services import fleet as fleet_svc
from app.services import items as items_svc
from tests.test_delegation import (  # noqa: F401 — fixtures
    _agent, _delegate, _mcp, _ok, _stored, db, key, proj)
from tests.test_review_withheld_is_named import _seat


URL = "https://github.com/o/r/pull/9"


def _to_review(client, key, planner, *, title: str, branch: str) -> str:
    item = _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]
    delegated = _ok(_delegate(client, key, item, planner, seat=True))
    builder = _agent(client, key, title, enrolment_code=delegated["enrolment_code"],
                     branch=branch, capabilities={"vendor": "cursor"})
    _ok(_mcp(client, key, "update_item", {
        "id": item, "status": "review", "agent_id": builder}))
    return item


def _reviewer(client, key, proj, db, planner, label: str) -> str:
    return _agent(client, key, label,
                  enrolment_code=_seat(db, proj, planner, review_only=True),
                  capabilities={"vendor": "anthropic"})


def test_claim_review_hands_the_linked_row_and_withholds_the_unlinked_one(
        client, key, proj, db):
    """The waiting row is created first, so sort_order would hand it out. It must not.

    Sabotage: delete the `pr_unlinked` branch in `review_withholding`. This claims
    the unlinked row.
    """
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    waiting = _to_review(client, key, planner, title="waiting", branch="gb/wait")
    ready = _to_review(client, key, planner, title="ready", branch="gb/ready")
    assert _stored(db, waiting).sort_order < _stored(db, ready).sort_order
    assert _stored(db, waiting).branch == "gb/wait"
    linked = _stored(db, ready)
    linked.github_url = URL
    db.commit()

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    withheld = fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj)
    assert [(w["item"], w["rule"]) for w in withheld] == [
        (waiting, fleet_svc.WITHHELD_PR_UNLINKED)]
    assert "no linked PR" in withheld[0]["reason"]

    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == ready, got

    # Recording the PR is what releases the row. The ready one is already held.
    row = _stored(db, waiting)
    row.github_url = "https://github.com/o/r/pull/10"
    db.commit()
    nxt = _reviewer(client, key, proj, db, planner, "reviewer-2")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": nxt}))
    assert got["claimed"] and got["item"]["id"] == waiting, got


def test_a_different_done_refusal_is_still_handed_out(client, key, proj, db):
    """`pr_missing` is the only refusal this gate holds. A different one still gets a reviewer."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="other", branch="gb/other")
    stored = _stored(db, item)
    stored.evidence = [{"kind": "note", "detail": "refused at `done` (adversarial): nope"}]
    db.commit()

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    assert fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj) == []
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item


def test_an_owner_close_without_a_pr_is_not_held_back(client, key, proj, db):
    """The no-PR gate does not apply to an owner close, so review must not either."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="closed", branch="gb/closed")
    stored = _stored(db, item)
    stored.evidence = [{
        "kind": "attestation",
        "adapter": items_svc.OWNER_CLOSE_ADAPTER,
        "commit": "abc1234def",
        "predicates": [{"name": "on_base", "passed": True, "detail": "owner closed"}],
    }]
    db.commit()

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got
