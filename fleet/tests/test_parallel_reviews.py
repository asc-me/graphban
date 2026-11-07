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


def _child(*held: str):
    return SimpleNamespace(held_items=list(held))


def test_a_reviewer_that_has_claimed_does_not_block_the_next():
    reviewer = _child("GRPH-1")
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
    claimed, fresh = _child("GRPH-1"), _child()
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
