from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "first")
    return root


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real repository with one commit, so `git worktree add` works."""
    return _init(tmp_path / "repo")


@pytest.fixture
def other_repo(tmp_path: Path) -> Path:
    return _init(tmp_path / "other")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A private state directory, so tests never touch the real one."""
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def linked_worktree(git_repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of `git_repo` — what the supervisor itself creates."""
    wt = tmp_path / "wt-a"
    _git(git_repo, "worktree", "add", "-q", "-b", "gb/wave-a", str(wt))
    return wt
