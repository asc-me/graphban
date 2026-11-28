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


# ---- GRPH-2006: an item whose only deliverable is ledger state -----------------------------

PRD_TEXT = ("GBC-P23 rebaselined v1.1 -> v1.2, grill answered; body_hash 49ef50d796f3048e")
CLAUSE = "GBC-P23 is rebaselined to v1.2"


def _ledger_item_to_review(client, key, planner, *, branch: str, touchpoints=None) -> str:
    """A planner task with no touchpoints, moved to review naming the PRD version it left.

    `branch` is the stale pointer GBC-299 inherited from the filing session's checkout.
    """
    item = _ok(_mcp(client, key, "create_item", {
        "title": "rebaseline GBC-P23", "status": "next",
        "description": f"## Acceptance\n- {CLAUSE}",
        **({"touchpoints": touchpoints} if touchpoints else {})}))["id"]
    delegated = _ok(_delegate(client, key, item, planner, seat=True))
    builder = _agent(client, key, "ledger-builder",
                     enrolment_code=delegated["enrolment_code"],
                     branch=branch, capabilities={"vendor": "cursor"})
    _ok(_mcp(client, key, "update_item", {
        "id": item, "status": "review", "agent_id": builder,
        "evidence": [{"kind": "note", "detail": PRD_TEXT}]}))
    return item


def test_a_touchpoint_less_prd_text_item_is_claimable_and_signs_off_naming_a_prd_version(
        client, key, proj, db, monkeypatch):
    """No touchpoints, a PRD version as the artifact: not withheld, and sign_off takes it.

    The cooldown is on, as on a deployment with CI: that is where sign_off refuses a branch
    with no PR (`pr_missing`). Sabotage: drop the `is_ledger_only` return in
    `_review_pr_unlinked`, or in `items._refuse_if_branch_has_no_pr` (re-applying the PR
    requirement to touchpoint-less items). Either one fails this.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _ledger_item_to_review(client, key, planner, branch="gbc-287-cloud-cta")
    stored = _stored(db, item)
    assert not stored.touchpoints and stored.branch == "gbc-287-cloud-cta"
    assert items_svc.is_ledger_only(stored)

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    assert fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj) == []
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got

    done = _ok(_mcp(client, key, "sign_off", {
        "id": item, "agent_id": reviewer,
        "evidence": [{"kind": "test", "detail": f"{CLAUSE}: get_prd GBC-P23 body_hash "
                                                 "49ef50d796f3048e, version v1.2"}]}))
    assert done["status"] == "done", done
    details = [e.get("detail", "") for e in _stored(db, item).evidence or []]
    assert not any("pr_missing" in d for d in details), details


def test_a_touchpoint_less_item_naming_no_prd_version_still_needs_its_pr(
        client, key, proj, db):
    """The exemption is the artifact, not the missing touchpoints alone."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _ok(_mcp(client, key, "create_item", {
        "title": "no artifact", "status": "next"}))["id"]
    delegated = _ok(_delegate(client, key, item, planner, seat=True))
    builder = _agent(client, key, "b", enrolment_code=delegated["enrolment_code"],
                     branch="gb/none", capabilities={"vendor": "cursor"})
    _ok(_mcp(client, key, "update_item", {
        "id": item, "status": "review", "agent_id": builder,
        "evidence": [{"kind": "note", "detail": "updated GBC-P23"}]}))
    assert not items_svc.is_ledger_only(_stored(db, item))
    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    withheld = fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj)
    assert [(w["item"], w["rule"]) for w in withheld] == [
        (item, fleet_svc.WITHHELD_PR_UNLINKED)]


def test_an_item_with_touchpoints_and_no_pr_is_still_withheld_pr_unlinked(
        client, key, proj, db):
    """Touchpoints claim code. A PRD-version note does not stand in for the PR."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _ledger_item_to_review(client, key, planner, branch="gb/code",
                                  touchpoints=["backend/app/x.py"])
    assert not items_svc.is_ledger_only(_stored(db, item))
    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    withheld = fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj)
    assert [(w["item"], w["rule"]) for w in withheld] == [
        (item, fleet_svc.WITHHELD_PR_UNLINKED)]


def test_a_planner_can_clear_an_items_branch_and_a_worker_cannot(client, key, proj, db):
    """`clear_branch` is the explicit clear; `branch=""` stays not-a-write."""
    planner = _agent(client, key, "planner", role_hint="planner",
                     capabilities={"vendor": "anthropic"})
    item = _ok(_mcp(client, key, "create_item", {"title": "stale", "status": "next"}))["id"]
    row = _stored(db, item)
    row.branch = "gbc-287-cloud-cta"
    db.commit()

    _ok(_mcp(client, key, "update_item", {"id": item, "branch": "", "agent_id": planner}))
    assert _stored(db, item).branch == "gbc-287-cloud-cta"

    worker = _agent(client, key, "worker", role_hint="worker",
                    capabilities={"vendor": "cursor"})
    refused = _mcp(client, key, "update_item",
                   {"id": item, "clear_branch": True, "agent_id": worker})
    assert refused.get("isError"), refused
    assert _stored(db, item).branch == "gbc-287-cloud-cta"

    _ok(_mcp(client, key, "update_item",
             {"id": item, "clear_branch": True, "agent_id": planner}))
    stored = _stored(db, item)
    assert stored.branch == ""
    assert any("branch cleared (was gbc-287-cloud-cta)" in str(e.get("detail"))
               for e in stored.evidence or [])
