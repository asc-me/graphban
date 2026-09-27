"""A branch and a PR carry exactly one item; salvage keeps items apart (GRPH-948).

Reported from SA-P21: one child built four items on one branch, salvage made one WIP commit
naming three, and the PR — "SA-583, SA-581, SA-580: …", 19 files, ~2,500 lines — merged before
review and needed four fix PRs. A cluster claim is a scheduling unit, not a delivery unit.

Sabotage: restore the joined-items title in `propose.describe` and the one-item-per-PR tests
here fail.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod
from gbfleet import worktree as wt_mod
from gbfleet.supervisor import Wave, propose_branch
from gbfleet.worktree import Worktree, choose_resume, orphans, reap_held, salvage_message


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout


def _commit(repo: Path, path: str, subject: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(subject + "\n", encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def published(git_repo: Path, tmp_path: Path) -> Path:
    """`git_repo` with a real bare `origin` holding `main`, so pushes land somewhere."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(git_repo, "remote", "add", "origin", str(bare))
    _git(git_repo, "push", "-q", "origin", "main:main")
    _git(git_repo, "fetch", "-q", "origin")
    return git_repo


@pytest.fixture
def forge(monkeypatch):
    """A recorded `gh`: every PR the supervisor opens, as (branch, base, title)."""
    opened: list[tuple[str, str, str]] = []
    monkeypatch.setattr(wt_mod, "default_ref", lambda repo, remote: "origin/main")

    def propose(repo, branch, base, *, title, body):
        opened.append((branch, base, title))
        return propose_mod.Proposed(branch=branch, url=f"https://x/pull/{len(opened)}", ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


class _Ledger:
    def __init__(self):
        self.evidence: list[tuple[str, dict]] = []

    def call(self, tool, **kw):
        if tool == "update_item":
            for e in kw.get("evidence") or []:
                self.evidence.append((kw["id"], e))
        return {}


# ---- describe ---------------------------------------------------------------------------------

def test_a_title_names_exactly_one_item():
    title, _ = propose_mod.describe("gb/w-1", ["SA-583"], "Add rotation")
    assert title == "SA-583: Add rotation"
    with pytest.raises(ValueError):
        propose_mod.describe("gb/w-1", ["SA-583", "SA-581"], "Add rotation")


# ---- a branch carrying several items becomes one branch and one PR per item -----------------

def test_a_child_holding_several_items_proposes_one_pr_per_item(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-1")
    a = _commit(repo, "web/a.tsx", "SA-580: board filter")
    _commit(repo, "web/a2.tsx", "tidy the filter")  # names nothing: belongs to SA-580
    b = _commit(repo, "web/b.tsx", "SA-581: card badge")
    _git(repo, "checkout", "-q", "main")
    ledger = _Ledger()

    wave = Wave()
    propose_branch(wave, repo, "gb/w-1", ["SA-580", "SA-581"], client=ledger)

    assert [(br, base) for br, base, _ in forge] == [
        ("gb/w-1--SA-580", "origin/main"),
        ("gb/w-1--SA-581", "origin/gb/w-1--SA-580"),
    ], wave.failures
    titles = [t for _, _, t in forge]
    assert titles == ["SA-580: tidy the filter", "SA-581: card badge"]
    for t in titles:
        assert sum(i in t for i in ("SA-580", "SA-581")) == 1, t
    # Each item's branch ends at its own last commit, and the stack keeps each diff to one item.
    assert _git(repo, "rev-parse", "gb/w-1--SA-581").strip() == b
    assert _git(repo, "merge-base", "--is-ancestor", a, "gb/w-1--SA-580") == ""
    assert "web/b.tsx" not in _git(repo, "diff", "--name-only", "main", "gb/w-1--SA-580")
    assert _git(repo, "diff", "--name-only", "gb/w-1--SA-580", "gb/w-1--SA-581").split() == ["web/b.tsx"]
    # The receipt lands on its own item and names its own PR.
    urls = {(i, e["url"]) for i, e in ledger.evidence if e.get("kind") == "url"}
    assert urls == {("SA-580", "https://x/pull/1"), ("SA-581", "https://x/pull/2")}
    assert not wave.failures, wave.failures


def test_commits_that_do_not_separate_the_items_are_not_proposed(published, forge):
    """Interleaved work cannot be stacked without rewriting it, and that is not the
    supervisor's call. Nothing is proposed, and the wave says why."""
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-2")
    _commit(repo, "a.py", "SA-580: one")
    _commit(repo, "b.py", "SA-581: two")
    _commit(repo, "c.py", "SA-580: three")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-2", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    assert "interleaved" in wave.unproposed["gb/w-2"]
    assert any("not proposed" in f for f in wave.failures)


def test_a_commit_naming_two_items_is_not_proposed(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-3")
    _commit(repo, "a.py", "SA-580, SA-581: both at once")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-3", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    assert "names SA-580, SA-581" in wave.unproposed["gb/w-3"]


def test_one_item_is_proposed_as_before(published, forge):
    """The control: the split is for several items, and one item keeps its own branch."""
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-4")
    _commit(repo, "a.py", "SA-580: one")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "push", "-q", "origin", "gb/w-4:gb/w-4")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-4", ["SA-580"], client=_Ledger())

    assert forge == [("gb/w-4", "origin/main", "SA-580: one")]


# ---- salvage writes one commit per item ---------------------------------------------------------

def test_salvage_message_refuses_two_items():
    assert salvage_message("fake", ["GRPH-1"]).endswith("items=GRPH-1")
    with pytest.raises(ValueError):
        salvage_message("fake", ["GRPH-1", "GRPH-2"])


def test_salvage_writes_one_commit_per_held_item(git_repo: Path, tmp_path: Path):
    path = tmp_path / "wt"
    _git(git_repo, "worktree", "add", "-q", "-b", "gb/w-9", str(path))
    base = _git(git_repo, "rev-parse", "HEAD").strip()
    (path / "work.py").write_text("half done\n", encoding="utf-8")

    reaped = reap_held(Worktree(path=path, branch="gb/w-9", repo=git_repo, base=base),
                       "fake", ["GRPH-1", "GRPH-2", "GRPH-3"])

    assert reaped.siblings == (("GRPH-2", "gb/w-9--GRPH-2"), ("GRPH-3", "gb/w-9--GRPH-3"))
    assert reaped.split_reason == ""
    subjects = _git(git_repo, "log", "--format=%s", "--branches=gb/*", f"^{base}").splitlines()
    assert len(subjects) == 3
    for s in subjects:
        assert s.count("GRPH-") == 1, f"a salvage subject lists two items: {s}"
    # Nothing is divided: each item's branch carries the whole of what was left.
    for br in ("gb/w-9", "gb/w-9--GRPH-2", "gb/w-9--GRPH-3"):
        assert _git(git_repo, "show", f"{br}:work.py") == "half done\n"
    # And each item resumes from its own branch.
    found = orphans(git_repo)
    rows = {k: {"status": "next", "claimed_by": None} for k in ("GRPH-1", "GRPH-2", "GRPH-3")}
    picked = {o.item_keys: o.branch for o in choose_resume(found, rows)}
    assert picked == {("GRPH-1",): "gb/w-9", ("GRPH-2",): "gb/w-9--GRPH-2",
                      ("GRPH-3",): "gb/w-9--GRPH-3"}


def test_salvaged_work_for_several_items_names_each_items_own_branch(published, forge, tmp_path):
    repo = published
    path = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "gb/w-8", str(path))
    base = _git(repo, "rev-parse", "HEAD").strip()
    (path / "work.py").write_text("half done\n", encoding="utf-8")
    reap_held(Worktree(path=path, branch="gb/w-8", repo=repo, base=base), "fake",
              ["GRPH-1", "GRPH-2"])
    ledger = _Ledger()

    wave = Wave()
    propose_branch(wave, repo, "gb/w-8", ["GRPH-1", "GRPH-2"], client=ledger)

    assert forge == [], "a salvage is never proposed"
    notes = {i: e["detail"] for i, e in ledger.evidence}
    assert "`gb/w-8`" in notes["GRPH-1"]
    assert "`gb/w-8--GRPH-2`" in notes["GRPH-2"]
    assert wave.published["gb/w-8--GRPH-2"].ok, "the sibling is pushed so it can be found"
