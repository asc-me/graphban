"""GRPH-1352 — resume the item's latest pushed head; generated files do not block a PR."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet.supervisor import Wave, _note_touchpoints, latest_head
from gbfleet.worktree import Orphan

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


def test_an_item_with_work_on_two_branches_resumes_from_the_later(tmp_path):
    """Sabotage: return `orphan` unchanged from latest_head and this fails."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "gb/p-38")
    (repo / "b").write_text("2")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "WIP: salvaged by gbfleet GRPH-1")
    salvage = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "gb/p-40")
    (repo / "c").write_text("3")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "more work")
    _git(repo, "checkout", "-q", "main")
    orphan = Orphan(branch="gb/p-38", commit=salvage, subject="s", salvaged=True,
                    item_keys=("GRPH-1",))
    assert latest_head(repo, orphan, {"branch": "gb/p-40"}).branch == "gb/p-40"


def test_an_unrelated_recorded_branch_does_not_replace_the_salvage(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "gb/p-38")
    (repo / "b").write_text("2")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "salvage")
    salvage = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "gb/other")
    orphan = Orphan(branch="gb/p-38", commit=salvage, subject="s", salvaged=True)
    assert latest_head(repo, orphan, {"branch": "gb/other"}) is orphan


class _Child:
    def __init__(self, branch, held):
        self.branch, self.held_items = branch, held


def test_generated_files_do_not_block_a_proposal_but_other_files_do():
    wave = Wave()
    wave.declared["GRPH-1"] = ["fleet/src"]
    wave.touched["gb/w-1"] = ["fleet/src/x.py", "AGENTS.md", ".claude/agents/README.md",
                              "backend/tests/shard_weights.json"]
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]), None)
    assert wave.undeclared == {}
    wave.touched["gb/w-2"] = ["fleet/src/x.py", "AGENTS.md", "web/src/App.tsx"]
    _note_touchpoints(wave, _Child("gb/w-2", ["GRPH-1"]), None)
    assert wave.undeclared == {"gb/w-2": ["web/src/App.tsx"]}
