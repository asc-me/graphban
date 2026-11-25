"""GRPH-1830: a review row the server says needs a person gets no reviewer and its own exit.

GRPH-1825 was a review row two reviewers had said NOT YET on (and one had set a blocker).
`until` kept exiting `review-unsigned`, the drain restarted it, and every cycle spawned review
children for a row `claim_review` could not move on — about 52 seats on one head.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import drain_outcome
from gbfleet.supervisor import Limits
from gbfleet.until import needs_person_line, review_needs_person, run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

WHY = ("review_skipped: 2 reviewers (GRPH-A1, GRPH-A2) said NOT YET on head aaaaaaaaaaaa "
       "and its acceptance has not changed since: needs a person")
PARKED = {"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A0",
          "review_claimed_by": "", "branch": "gb/x", "evidence": [],
          "review_needs_person": WHY}


def _go(git_repo, tmp_path, scripts, state, review):
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(workspace, review=review, minted_roles=roles)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"), planner, supervisor,
        api_key=KEY, server="http://gb.invalid", adapter="fake", state=state,
        workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    return result, roles


def test_a_parked_row_spawns_no_reviewer_and_exits_needs_person(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Sabotage: keep needs-person rows in `rows`. A reviewer is minted and it is `review-unsigned`."""
    result, roles = _go(git_repo, tmp_path, scripts, state, [dict(PARKED)])
    assert roles == []  # no review seat minted, so no review child
    assert result.reason == "review-needs-person"
    assert result.detail == f"review-needs-person: GRPH-9: {WHY}"
    assert f"review-needs-person: GRPH-9: {WHY}" in result.wave.review_waits


def test_a_blocked_row_reads_the_same(git_repo: Path, tmp_path: Path, scripts, state: Path):
    row = dict(PARKED, blocker="Needs a person: live check",
               review_needs_person="review_skipped: blocked by a person-only step: "
                                   "Needs a person: live check")
    result, roles = _go(git_repo, tmp_path, scripts, state, [row])
    assert roles == []
    assert result.reason == "review-needs-person"
    assert "blocked by a person-only step" in result.detail


def test_a_row_without_the_field_still_gets_a_reviewer():
    """An older server sends no `review_needs_person`: the row is reviewable, as before."""
    row = {k: v for k, v in PARKED.items() if k != "review_needs_person"}
    assert review_needs_person(row) == ""


def test_the_drain_reports_it_as_a_stall_naming_the_person_step():
    line = needs_person_line(PARKED)
    out = drain_outcome.classify(
        "review-needs-person",
        [drain_outcome.OpenItem(id="GRPH-9", status="review")],
        review_waits=[line])
    assert out.state == drain_outcome.STALLED
    assert out.reasons["GRPH-9"] == f"needs a person: {WHY}"
