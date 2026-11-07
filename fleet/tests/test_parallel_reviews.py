"""GRPH-1413: a reviewer that has claimed its rows no longer holds every other review row.

`until` gated each review spawn on "is any reviewer of ours alive". On the P73 drain that
queued six review rows behind one 15-30 minute reviewer at a time, though the rows shared
no files. Only a reviewer that has not claimed yet should block another: it has not said
which rows it is taking (GRPH-1374).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gbfleet import until as until_mod
from gbfleet.supervisor import Limits
from gbfleet.until import _review_busy, run

from tests.test_until import KEY, _clients, _factory


def _child(*held: str, reviewing: tuple = ()):
    return SimpleNamespace(held_items=list(held), review_items=list(reviewing))


def test_a_reviewer_that_has_claimed_does_not_block_the_next():
    reviewer = _child(reviewing=("GRPH-1",))
    assert _review_busy([reviewer], [reviewer]) is False


def test_a_reviewer_that_has_not_claimed_still_blocks_the_next():
    reviewer = _child()
    assert _review_busy([reviewer], [reviewer]) is True


def test_an_empty_hold_reads_as_not_claimed():
    reviewer = _child("", "")
    assert _review_busy([reviewer], [reviewer]) is True


def test_a_live_builder_never_blocks_a_review_spawn():
    builder = _child()
    assert _review_busy([builder], []) is False


def test_one_unclaimed_reviewer_among_claimed_ones_blocks():
    claimed, fresh = _child(reviewing=("GRPH-1",)), _child()
    assert _review_busy([claimed, fresh], [claimed, fresh]) is True


def test_the_loop_asks_review_busy_with_the_children_it_spawned_for_review(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The wiring, not just the helper: the review branch of `run` decides through
    `_review_busy`, handing it the live children and the reviewers it spawned."""
    calls: list[tuple[int, int]] = []
    real = until_mod._review_busy

    def spy(live, review_spawned):
        calls.append((len(live), len(review_spawned)))
        return real(live, review_spawned)

    monkeypatch.setattr(until_mod, "_review_busy", spy)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": ""}],
    )
    run(
        git_repo, _factory(scripts, "works_then_waits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert calls, "the review branch never consulted _review_busy"
    assert any(spawned >= 1 for _, spawned in calls), "the spawned reviewer was never passed in"


def test_a_review_claim_from_the_roster_reaches_review_busy():
    """The real path: a review-only seat's roster entry carries `hold: review` and no build
    lease. `build_holdings` drops it, so `held_items` stays empty; the claim must still be
    read, or every live reviewer looks unclaimed and until stays one-at-a-time."""
    from gbfleet.spawn import Child
    from gbfleet.supervisor import Partition, _holdings, _remember_holdings, review_holdings

    roster = {"agents": [{"id": "A1", "holdings": [{"id": "GRPH-9", "hold": "review"}]}]}
    part = Partition()
    part.held = _holdings(roster)
    part.reviewing = {a["id"]: [h["id"] for h in review_holdings(a)] for a in roster["agents"]}
    child = Child.__new__(Child)
    child.agent_id, child.held_items, child.ever_held, child.review_items = "A1", [], [], []
    _remember_holdings([child], part)
    assert child.held_items == [], "a review claim must not become a salvageable lease"
    assert child.review_items == ["GRPH-9"]
    assert _review_busy([child], [child]) is False
    part.reviewing = {"A1": []}
    _remember_holdings([child], part)
    assert _review_busy([child], [child]) is True
