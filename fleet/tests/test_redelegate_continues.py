"""GRPH-1390 — a re-delegated item continues from its last committed branch, never main.

The GRPH-1360 case: attempt 3 committed a complete build on its branch and exited without
moving the item to review. The row is `next`, carries the branch, has no bounce reason and
no `WIP: salvaged` subject, so the next delegation was cut from the base and rebuilt it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import worktree as wt

_CFG = ["-c", "user.email=a@b.c", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("1")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "base")
    return repo


def _committed_attempt(repo: Path, branch: str) -> str:
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "built").write_text("work")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "complete build")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _row(branch: str, **extra) -> dict:
    return {"status": "next", "claimed_by": "", "branch": branch, "bounce_reason": "", **extra}


def test_committed_attempt_without_bounce_is_continued(tmp_path):
    """Sabotage: make prior_branch_of return "" and the new branch lacks the commit."""
    repo = _repo(tmp_path)
    prior = _committed_attempt(repo, "gb/w-3")
    row = _row("gb/w-3")
    assert wt.bounce_branch_of(row) == ""
    branch = wt.prior_branch_of(repo, row, "main")
    assert branch == "gb/w-3"
    tree = wt.resume_bounced(repo, tmp_path / "wt", branch, base="main")
    assert tree.branch == "gb/w-3"
    assert _git(tree.path, "merge-base", "--is-ancestor", prior, "HEAD") == ""
    assert (tree.path / "built").read_text() == "work"


def test_branch_with_nothing_beyond_base_is_a_fresh_cut(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "gb/w-1")
    assert wt.prior_branch_of(repo, _row("gb/w-1"), "main") == ""


def test_explicit_start_over_is_honoured(tmp_path):
    repo = _repo(tmp_path)
    _committed_attempt(repo, "gb/w-3")
    row = _row("gb/w-3", bounce_reason="wrong approach; start over")
    assert wt.prior_branch_of(repo, row, "main") == ""


def test_missing_branch_or_row_is_a_fresh_cut(tmp_path):
    repo = _repo(tmp_path)
    assert wt.prior_branch_of(repo, None, "main") == ""
    assert wt.prior_branch_of(repo, _row("gb/gone-9"), "main") == ""
    assert wt.prior_branch_of(repo, _row(""), "main") == ""
