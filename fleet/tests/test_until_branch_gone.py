"""GRPH-1010 — a review row whose branch was deleted on merge spawns no reviewer.

A reviewer loop pointed at four merged items spawned nine children across three cycles,
each `unreported`, each cycle exiting `review-unsigned`: GitHub had deleted the branches
when the PRs merged, so every child was handed something it could not read.

The loop now asks the remote. A row whose branch is gone and whose merge commit cannot be
found in the base is recorded on the item (naming the branch), kept away from a reviewer,
and reported as a wait. One whose merge commit IS in the base stays reviewable: the receipt
carries that commit, and the server offers it to be reviewed there.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import until
from gbfleet.supervisor import Limits
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients, _repo_with_remote

PR = "https://github.com/o/r/pull/77"


def _row(branch: str = "gb/grph-9", **extra) -> dict:
    return {"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
            "review_claimed_by": "", "branch": branch, "pr": PR, "evidence": [], **extra}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_a_deleted_branch_with_no_merge_commit_spawns_no_reviewer(
    tmp_path: Path, scripts, state: Path,
):
    """The whole shape: item in review, branch deleted, the loop makes no child.

    Sabotage: in `_gone_branches`, keep the row (`kept.append(row)`) when no merge commit
    is found. The loop mints review seats again and exits `review-unsigned`.
    """
    repo = _repo_with_remote(tmp_path)
    workspace = tmp_path / "ws"
    roles: list[str] = []
    updates: list[dict] = []
    planner, supervisor = _clients(
        workspace, review=[_row()], minted_roles=roles, updates=updates)
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles == [], f"a reviewer was spawned into a deleted branch: {roles}"
    assert result.reason != "review-unsigned", result.as_json()
    gone = [u for u in updates if u.get("id") == "GRPH-9"
            and any(until.BRANCH_GONE_PREFIX in str(e.get("detail") or "")
                    for e in u.get("evidence") or [])]
    assert gone, f"the absence was not recorded on the item: {updates}"
    detail = gone[0]["evidence"][0]["detail"]
    assert "gb/grph-9" in detail and "origin" in detail, detail
    assert "status" not in gone[0], "recording the absence must not move the item"
    assert result.reason == "idle-with-waits", result.as_json()
    assert any("gb/grph-9" in w and "nothing for a reviewer to read" in w
               for w in result.waits), result.waits


def test_a_branch_still_on_the_remote_is_left_alone(tmp_path: Path):
    repo = _repo_with_remote(tmp_path)
    _git(repo, "push", "-q", "origin", "main:gb/grph-9")
    updates: list = []

    class Planner:
        def call(self, tool, **kw):
            updates.append((tool, kw))

    kept, waits = until._gone_branches(Planner(), [_row()], repo, "main", {}, "GRPH-P1")
    assert kept == [_row()] and waits == [] and updates == []


def test_a_row_with_no_pr_is_never_called_gone(tmp_path: Path):
    """A branch never pushed is publish_pending, not gone."""
    repo = _repo_with_remote(tmp_path)
    row = _row(pr="")
    kept, waits = until._gone_branches(None, [row], repo, "main", {}, "GRPH-P1")
    assert kept == [row] and waits == []


def test_a_merge_commit_in_the_base_keeps_the_row_and_names_the_commit(tmp_path: Path):
    """GitHub's squash subject ends `(#77)`. The row stays reviewable at that commit."""
    repo = _repo_with_remote(tmp_path)
    (repo / "work.txt").write_text("shipped\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "GRPH-9: the work (#77)")
    merge = _git(repo, "rev-parse", "HEAD")
    calls: list = []

    class Planner:
        def call(self, tool, **kw):
            calls.append((tool, kw))

    kept, waits = until._gone_branches(Planner(), [_row()], repo, "main", {}, "GRPH-P1")
    assert kept == [_row()] and waits == []
    assert len(calls) == 1 and calls[0][0] == "update_item"
    receipt = calls[0][1]["evidence"][0]
    assert receipt["kind"] == "url" and receipt["commit"] == merge
    assert receipt["detail"].startswith(f"{until.BRANCH_GONE_PREFIX} gb/grph-9 is not on origin")

    # Once the receipt is on the item, it is not written again.
    row = _row(evidence=[receipt])
    until._gone_branches(Planner(), [row], repo, "main", {}, "GRPH-P1")
    assert len(calls) == 1
