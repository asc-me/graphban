"""GRPH-1010 — an item whose branch is gone is not offered to a reviewer that cannot read it.

GitHub deletes a branch when its PR merges. Four items sat in `review` with their work on
`main`, and a reviewer loop spawned nine children in three cycles against them: each was
handed a branch that no longer existed and could only report it unreadable.

gbfleet records the absence on the item (`gbfleet: branch gone: <branch> is not on <remote>`).
With no merge commit beside it the row is withheld (`branch_gone`) and the reason names the
branch. With the merge commit, the row is offered, `claim_review` names that commit, and a
reviewer signs it off at that commit: merged work reaches `done` through a review, not a
person writing the status.
"""
from __future__ import annotations

from app.services import fleet as fleet_svc
from tests.test_delegation import (  # noqa: F401 — fixtures
    _agent, _delegate, _mcp, _ok, _stored, db, key, proj)
from tests.test_pr_unlinked_review import _reviewer, _to_review

URL = "https://github.com/o/r/pull/41"
MERGE = "d4a88f3ad28e4f997ca63d962de8844eab25e38e"
WHY = "signed at the squash merge on main; its branch was deleted when the PR merged"


def _gone(db, item, *, commit: str = "", branch: str = "gb/gone") -> None:
    row = _stored(db, item)
    row.github_url = URL
    receipt = ({"kind": "url", "url": URL, "commit": commit,
                "detail": f"{fleet_svc.BRANCH_GONE_PREFIX} {branch} is not on origin; its work "
                          f"is in main as {commit[:12]}"}
               if commit else
               {"kind": "note", "url": "",
                "detail": f"{fleet_svc.BRANCH_GONE_PREFIX} {branch} is not on origin, and no "
                          "merge commit for it was found in main"})
    row.evidence = list(row.evidence or []) + [receipt]
    db.commit()


def test_a_gone_branch_with_no_merge_commit_is_withheld_and_named(client, key, proj, db):
    """Sabotage: delete the `branch_gone` branch in `review_withholding`. The reviewer
    is handed the row and the first assertion on `claimed` fails."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="merged", branch="gb/gone")
    _gone(db, item)

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert not got["claimed"], got
    withheld = got["withheld"]
    assert [(w["item"], w["rule"]) for w in withheld] == [
        (item, fleet_svc.WITHHELD_BRANCH_GONE)], withheld
    assert "gb/gone" in withheld[0]["reason"], "the refusal must name the branch that is gone"
    assert "gb/gone" in got["reason"]


def test_a_receipt_for_another_branch_does_not_withhold(client, key, proj, db):
    """A rebuild on a new branch is readable. The old branch's absence says nothing about it."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="rebuilt", branch="gb/new")
    _gone(db, item, branch="gb/old")

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got
    assert got["merge_commit"] == ""


def test_new_work_on_the_branch_clears_the_receipt(client, key, proj, db):
    """A builder's later `gbfleet: committed` note means the branch was pushed again."""
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="again", branch="gb/gone")
    _gone(db, item)
    row = _stored(db, item)
    row.evidence = list(row.evidence) + [{
        "kind": "note", "url": "",
        "detail": "gbfleet: committed 1234567890abcdef1234567890abcdef12345678 on gb/gone"}]
    db.commit()
    assert fleet_svc.branch_gone(_stored(db, item)) is None


def test_merged_work_reaches_done_through_a_review_of_its_merge_commit(client, key, proj, db):
    """The route chosen: review the merge commit. No human writes the status.

    Sabotage: make `branch_gone` return `(branch, "")` always. The row is withheld and
    the claim below fails.
    """
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="shipped", branch="gb/gone")
    _gone(db, item, commit=MERGE)

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got
    assert got["merge_commit"] == MERGE, "the reviewer must be told what to read instead"

    out = _ok(_mcp(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": MERGE,
        "waive": {"commit_is_not_the_base": WHY},
        "evidence": [{"kind": "test", "detail": f"read git show {MERGE[:12]} on main"}]}))
    assert out["status"] == "done", out
    stored = _stored(db, item)
    assert stored.status == "done"
    assert stored.reviewed_by == reviewer
    signed = [e for e in stored.evidence
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    assert signed and signed[-1]["commit"] == MERGE
