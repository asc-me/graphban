"""GRPH-1419: a PR is titled after the build commit, not the branch's newest commit."""
import subprocess
from pathlib import Path

from gbfleet import propose


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                   check=True, capture_output=True)


def _commit(repo: Path, name: str, msg: str) -> None:
    (repo / name).write_text(msg)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", msg)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _commit(tmp_path, "a", "base")
    return tmp_path


def test_multi_commit_branch_is_titled_after_the_build(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "work")
    _commit(repo, "b", "build X (GRPH-9)")
    _git(repo, "checkout", "main")
    _commit(repo, "c", "main moved")
    _git(repo, "checkout", "work")
    _git(repo, "merge", "--no-ff", "-m", "Merge main into work", "main")
    _commit(repo, "d", "fix a typo (GRPH-9)")
    assert propose.title_subject(repo, "work", "main") == "build X (GRPH-9)"
    title, _ = propose.describe("work", ["GRPH-9"], propose.title_subject(repo, "work", "main"))
    assert title == "build X (GRPH-9)"


def test_single_commit_branch_keeps_its_title(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "work")
    _commit(repo, "b", "only commit (GRPH-9)")
    assert propose.title_subject(repo, "work", "main") == propose.subject(repo, "work", "main")
