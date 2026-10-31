"""A branch behind the trunk is proposed when that does not matter (GRPH-1087), and
brought up to date or sent back when it does (GRPH-1236).

`_refuse_proposal` used to refuse every branch `_note_staleness` found behind the
default ref. The count does not say whether the missed commits touch this branch.
No shared files and a clean `git merge-tree --write-tree`: propose as is, and say
"N behind ref, no shared files, merges clean" in the PR body and the BEHIND line.

Shared files that still merge clean were refused too, and the item went on to `done`
with no PR. Now the trunk is merged in with a real merge commit, pushed, and the PR
opened. A real conflict withholds the PR and sends the item back to `next` with a
resume brief. A check that could not be asked still refuses.

Sabotage (GRPH-1236): in `_classify_behind`, put back the refusal for shared files
(`wave.behind_block[...] = "... rebase before proposing"` instead of recording
`behind_merge`). The shared-file test fails: no merge commit, no PR.
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


class _Ledger:
    """The observation route, recorded. The supervisor client may post nothing else."""

    def __init__(self):
        self.posted: list[dict] = []

    def post_observation(self, **payload):
        self.posted.append(payload)
        return {"recorded": True}


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


def test_behind_touching_one_of_its_files_is_merged_up_and_proposed(tmp_path, opened):
    """CASE (a). Same file, different lines: merge-tree is clean, and the missed commit
    changes a file the branch also changes. The trunk is merged in with a real merge
    commit (parents: the branch head and the ref), the branch is pushed, and the PR
    opens. The PR body and the item's evidence name the ref and the sha merged.
    """
    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    _git(repo, "push", "-q", "origin", "gb/w-1")
    head = _git(repo, "rev-parse", "gb/w-1").strip()
    _trunk_moves(origin, tmp_path, {"a.txt": "line1\nline2\nMAIN\n"}, "edit line 3")

    wave = _measured(repo, base)
    assert wave.stale == {"gb/w-1": (1, "origin/main")}
    ledger = _Ledger()
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=ledger)

    trunk = _git(repo, "rev-parse", "origin/main").strip()
    tip = _git(repo, "rev-parse", "gb/w-1").strip()
    parents = _git(repo, "rev-list", "--parents", "-n", "1", tip).split()[1:]
    assert parents == [head, trunk], "a real merge commit, never a rebase"
    assert _git(origin, "rev-parse", "gb/w-1").strip() == tip, "the merge was pushed"
    assert (repo / "a.txt").read_text() == "BRANCH\nline2\nMAIN\n"

    assert opened, (wave.proposed.get("gb/w-1"), wave.failures)
    note = (f"merged origin/main@{trunk[:12]} into the branch before proposing "
            "(1 behind; shared files: a.txt)")
    assert note in opened[0]
    assert wave.proposed["gb/w-1"].url
    assert "gb/w-1" not in wave.unproposed
    assert {"item_id": "GRPH-1", "kind": "merged_trunk", "branch": "gb/w-1",
            "reason": note} in ledger.posted

    out = io.StringIO()
    report(wave, out=out)
    assert "BEHIND gb/w-1: cut from a base 1 commit(s) behind origin/main; " + note \
        in out.getvalue(), "being behind is still reported"


def test_a_failed_merge_up_refuses_with_that_reason(tmp_path, opened, monkeypatch):
    """The merge or the push failing falls back to the refusal, with why."""
    import gbfleet.worktree as wt_mod

    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    _trunk_moves(origin, tmp_path, {"a.txt": "line1\nline2\nMAIN\n"}, "edit line 3")
    wave = _measured(repo, base)
    monkeypatch.setattr(wt_mod, "merge_ref_into", lambda repo, branch, ref: wt_mod.Merged(
        branch=branch, ref=ref, reason="git merge origin/main failed: boom"))
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=None)

    assert opened == []
    reason = wave.proposed["gb/w-1"].reason
    assert "missed commits also change a.txt" in reason
    assert "git merge origin/main failed: boom" in reason


def test_a_merge_tree_conflict_is_refused_and_sent_back_to_next(tmp_path, opened):
    """CASE (b). The same line, both sides. merge-tree exits 1, the PR is withheld,
    and the item is sent back to `next` with a brief naming the conflicted file.
    Nothing is merged into the branch."""
    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    head = _git(repo, "rev-parse", "gb/w-1").strip()
    _trunk_moves(origin, tmp_path, {"a.txt": "MAIN\nline2\nline3\n"}, "edit line 1 too")

    wave = _measured(repo, base)
    ledger = _Ledger()
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=ledger)

    assert opened == []
    reason = wave.proposed["gb/w-1"].reason
    assert "merge-tree conflicts in a.txt" in reason
    assert "1 commit(s) behind origin/main" in reason
    assert "gb/w-1" not in wave.behind_clean
    assert _git(repo, "rev-parse", "gb/w-1").strip() == head
    sent = [p for p in ledger.posted if p["kind"] == "behind_conflict"]
    assert sent == [{
        "item_id": "GRPH-1", "kind": "behind_conflict", "branch": "gb/w-1",
        "reason": ("Branch gb/w-1 conflicts with origin/main (a.txt). Next attempt: "
                   "resume from gb/w-1, `git merge origin/main` (no rebase, no "
                   "force-push), resolve keeping both sides, rerun tests, hand to review."),
    }]


def test_a_merge_tree_that_cannot_be_asked_still_refuses(tmp_path, opened, monkeypatch):
    """"Could not ask" is not "clean": no merge, no PR, no send-back."""
    import gbfleet.worktree as wt_mod

    origin, repo, base = _remote_repo(tmp_path)
    _on_branch(repo, {"a.txt": "BRANCH\nline2\nline3\n"}, "GRPH-1: edit line 1")
    head = _git(repo, "rev-parse", "gb/w-1").strip()
    _trunk_moves(origin, tmp_path, {"a.txt": "line1\nline2\nMAIN\n"}, "edit line 3")
    monkeypatch.setattr(wt_mod, "conflicted_paths", lambda repo, ref, branch: None)

    wave = _measured(repo, base)
    ledger = _Ledger()
    propose_branch(wave, repo, "gb/w-1", ["GRPH-1"], client=ledger)

    assert opened == []
    assert "merge-tree could not be asked, so not proposing" in wave.proposed["gb/w-1"].reason
    assert _git(repo, "rev-parse", "gb/w-1").strip() == head
    assert not [p for p in ledger.posted if p["kind"] in ("behind_conflict", "merged_trunk")]


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


def test_a_split_branch_does_not_carry_the_behind_note_onto_its_per_item_prs(tmp_path, opened):
    """A multi-item branch that is behind but clean is split (GRPH-1086), and each
    per-item branch is replayed onto the CURRENT trunk, so none of them is behind.
    A per-item PR that repeated the parent's "N behind" note would be stating a
    staleness its own diff does not have."""
    origin, repo, base = _remote_repo(tmp_path)
    # The split cherry-picks each item onto the trunk, which commits, so the clone needs
    # an identity of its own; a CI runner has no global one.
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "t")
    _on_branch(repo, {"b.txt": "branch\n"}, "GRPH-1: add b")
    (repo / "d.txt").write_text("other item\n")
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", "GRPH-2: add d")
    _trunk_moves(origin, tmp_path, {"c.txt": "trunk\n"}, "unrelated")

    wave = _measured(repo, base)
    assert wave.behind_clean.get("gb/w-1")

    propose_branch(wave, repo, "gb/w-1", ["GRPH-1", "GRPH-2"], client=None)
    assert len(opened) == 2, (wave.proposed, wave.failures)
    for body in opened:
        assert "behind" not in body, body
