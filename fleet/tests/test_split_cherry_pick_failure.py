"""A cherry-pick that is not a conflict is not a dependency (GRPH-1093).

`replay_onto` used to report every non-zero `git cherry-pick` as
"depends on an earlier item's commit". A missing committer identity fails the
same way — git applies the patch, leaves CHERRY_PICK_HEAD, and then refuses to
commit — and the supervisor told the operator to look at their history.

Sabotage: map every cherry-pick failure in `replay_onto` back to the dependency
reason. The no-identity test must fail.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod
from gbfleet import worktree as wt_mod
from gbfleet.supervisor import Wave, propose_branch


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout


def _commit(repo: Path, path: str, text: str, subject: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def published(git_repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(git_repo, "remote", "add", "origin", str(bare))
    _git(git_repo, "push", "-q", "origin", "main:main")
    _git(git_repo, "fetch", "-q", "origin")
    return git_repo


@pytest.fixture
def forge(monkeypatch):
    opened: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(wt_mod, "default_ref", lambda repo, remote: "origin/main")

    def propose(repo, branch, base, *, title, body, **_kw):
        opened.append((branch, base, title, body))
        return propose_mod.Proposed(branch=branch, url=f"https://x/pull/{len(opened)}", ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


class _Ledger:
    def call(self, tool, **kw):
        return {}

    def post_observation(self, **kw):
        return {"recorded": True}


def _strip_identity(monkeypatch, repo: Path, tmp_path: Path) -> None:
    """No committer anywhere git looks: repo, home, system, or the environment.

    The reported failure reproduced with `HOME=$(mktemp -d) GIT_CONFIG_NOSYSTEM=1`.
    """
    home = tmp_path / "no-git-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in ("user.email", "user.name"):
        subprocess.run(["git", "config", "--unset-all", key], cwd=str(repo),
                       capture_output=True, text=True)


def test_no_git_identity_is_not_reported_as_a_dependency(published, forge, monkeypatch, tmp_path):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-id")
    _commit(repo, "a.txt", "from A\n", "SA-580: add a")
    _commit(repo, "b.txt", "from B\n", "SA-581: add b")
    _git(repo, "checkout", "-q", "main")
    _strip_identity(monkeypatch, repo, tmp_path)

    wave = Wave()
    propose_branch(wave, repo, "gb/w-id", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    reason = wave.unproposed["gb/w-id"]
    assert "depends on an earlier item's commit" not in reason, reason
    assert "could not cut SA-581" in reason, reason
    assert "email" in reason.lower(), reason


def test_a_line_edited_from_an_earlier_item_is_still_a_dependency(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-dep")
    _commit(repo, "README.md", "x\nline from A\n", "SA-580: add a line")
    b = _commit(repo, "README.md", "x\nline from A and B\n", "SA-581: edit the line A added")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-dep", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    reason = wave.unproposed["gb/w-dep"]
    assert b[:12] in reason
    assert "depends on an earlier item's commit" in reason
    assert "could not cut" not in reason
