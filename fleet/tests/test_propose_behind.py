"""A branch behind the trunk is proposed when that does not matter (GRPH-1087).

`_refuse_proposal` used to refuse every branch `_note_staleness` found behind the
default ref. The count does not say whether the missed commits touch this branch.
Refuse only when they change a file the branch also changes, or when
`git merge-tree --write-tree` reports a conflict. Otherwise propose, and say
"N behind ref, no shared files, merges clean" in the PR body and the BEHIND line.

Sabotage: in `_classify_behind`, force the overlap check to return no shared files
(`shared = []` instead of `shared_with_missed`). The overlapping-file test must
fail — that fixture merges clean, so only the overlap check refuses it. Leaving
the call out and refusing every stale branch fails the disjoint test instead,
which is the other way this gate goes blind.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from gbfleet.cli import report
from gbfleet.supervisor import Wave, _note_staleness, propose_branch
from gbfleet.worktree import Worktree

_CFG = ["-c", "user.email=a@b.c", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done.stdout


def _remote_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    (repo / "a.txt").write_text("line1\nline2\nline3\n")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD").strip()
    return origin, repo, base


def _on_branch(repo: Path, files: dict[str, str], message: str) -> None:
    _git(repo, "checkout", "-q", "-b", "gb/w-1")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", message)


def _trunk_moves(origin: Path, tmp_path: Path, files: dict[str, str], message: str) -> None:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    for name, text in files.items():
        path = other / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(other, "add", "-A")
    _git(other, *_CFG, "commit", "-qm", message)
    _git(other, "push", "-q", "origin", "main")


def _measured(repo: Path, base: str) -> Wave:
    wave = Wave()
    _note_staleness(wave, Worktree(path=repo, branch="gb/w-1", repo=repo, base=base))
    return wave


@pytest.fixture
def opened(monkeypatch):
    import gbfleet.propose as propose_mod

    got: list[str] = []

    def fake(repo, branch, base, *, title, body):
        got.append(body)
        return propose_mod.Proposed(branch=branch, url="https://example.test/pull/1", ok=True)

    monkeypatch.setattr(propose_mod, "propose", fake)
    return got


def test_behind_with_no_shared_files_is_proposed_and_the_note_says_so(tmp_path, opened):
    """THE CHECK. One unrelated commit landed on the trunk during the build.
    The merge is clean and no file is in both diffs, so the PR is opened and
    both the body and the BEHIND line say why it was not refused."""
    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"b.txt": "branch\n"}, "GRPH-1: add b")
    _trunk_moves(origin, tmp_path, {"c.txt": "trunk\n"}, "unrelated")

    wave = _measured(repo, base)
    assert wave.stale == {"gb/w-1": (1, "origin/main")}
    note = "1 behind origin/main, no shared files, merges clean"
    assert wave.behind_clean == {"gb/w-1": note}
    assert "gb/w-1" not in wave.behind_block

    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=None)
    assert opened, wave.proposed.get("gb/w-1")
    assert note in opened[0]
    assert wave.proposed["gb/w-1"].url
    assert "gb/w-1" not in wave.unproposed

    out = io.StringIO()
    report(wave, out=out)
    assert (
        "BEHIND gb/w-1: cut from a base 1 commit(s) behind origin/main; " + note
    ) in out.getvalue()


def test_behind_touching_one_of_its_files_is_refused(tmp_path, opened):
    """Same file, different lines: the merge is clean and the branch is still
    refused, because the missed commit changes a file the branch also changes.

    Sabotage: force the overlap check to return no shared files. This must fail.
    The hunks do not conflict, so a gate that only asks merge-tree proposes it.
    """
    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    _trunk_moves(origin, tmp_path, {"a.txt": "line1\nline2\nMAIN\n"}, "edit line 3")

    wave = _measured(repo, base)
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=None)

    assert opened == []
    reason = wave.proposed["gb/w-1"].reason
    assert wave.proposed["gb/w-1"].skipped
    assert "1 commit(s) behind origin/main" in reason
    assert "rebase before proposing" in reason
    assert "a.txt" in reason
    assert "merge-tree conflicts" not in reason
    assert "gb/w-1" not in wave.behind_clean


def test_a_merge_tree_conflict_is_refused(tmp_path, opened):
    """The same line, both sides. merge-tree exits 1 and the PR is withheld.
    Dropping the merge-tree call leaves only the overlap reason, and this fails.
    """
    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    _trunk_moves(origin, tmp_path, {"a.txt": "MAIN\nline2\nline3\n"}, "edit line 1 too")

    wave = _measured(repo, base)
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=None)

    assert opened == []
    reason = wave.proposed["gb/w-1"].reason
    assert "merge-tree conflicts" in reason
    assert "1 commit(s) behind origin/main" in reason
    assert "gb/w-1" not in wave.behind_clean


def test_a_behind_check_that_cannot_see_the_branch_does_not_propose(tmp_path, opened):
    """The branch name on the worktree does not resolve. "Could not list files"
    is not "no shared files", and the PR stays withheld."""
    _origin, repo, base = _remote_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True,
    )
    (other / "c.txt").write_text("trunk\n")
    _git(other, "add", "-A")
    _git(other, *_CFG, "commit", "-qm", "unrelated")
    _git(other, "push", "-q", "origin", "main")

    wave = _measured(repo, base)
    assert wave.stale["gb/w-1"] == (1, "origin/main")
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=None)
    assert opened == []
    assert "could not list" in wave.proposed["gb/w-1"].reason
    assert "gb/w-1" not in wave.behind_clean
