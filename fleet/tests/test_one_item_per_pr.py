"""A branch and a PR carry exactly one item; salvage keeps items apart (GRPH-948, GRPH-1086).

Reported from SA-P21: one child built four items on one branch, salvage made one WIP commit
naming three, and the PR — "SA-583, SA-581, SA-580: …", 19 files, ~2,500 lines — merged before
review and needed four fix PRs. A cluster claim is a scheduling unit, not a delivery unit.

GRPH-1086: stacking those PRs still put the later item's commits on top of the earlier
item's, so one merge carried both. Each PR is cut from the base, carries only the commits
whose subject names that item, and says `Attests:` for that item alone. A commit that names
no item, names two, or will not apply without the other item's commit refuses the branch.

Sabotage: restore the joined-items title in `propose.describe` and the one-item-per-PR tests
here fail. Drop `attest=True` on the per-item `_open_pr` and the `Attests:` assertions fail.
Drop the "names no item" return in `split_by_item` and the unnamed-commit test fails.
"""
from __future__ import annotations

import re
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


def _attested(body: str) -> list[str]:
    """The ids on the `Attests:` line, the way CI reads it."""
    match = re.search(r"^[ \t]*Attests:[ \t]*(.*)$", body or "", re.MULTILINE)
    assert match, body
    return re.findall(r"\b[A-Z]{2,10}-\d+\b", match.group(1))


@pytest.fixture
def forge(monkeypatch):
    """A recorded `gh`: every PR the supervisor opens, as (branch, base, title, body)."""
    opened: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(wt_mod, "default_ref", lambda repo, remote: "origin/main")

    def propose(repo, branch, base, *, title, body):
        opened.append((branch, base, title, body))
        return propose_mod.Proposed(branch=branch, url=f"https://x/pull/{len(opened)}", ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


class _Ledger:
    def __init__(self):
        self.observations: list[dict] = []

    def call(self, tool, **kw):
        return {}

    def post_observation(self, **kw):
        self.observations.append(kw)
        return {"recorded": True}


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
    b = _commit(repo, "web/b.tsx", "SA-581: card badge")
    _git(repo, "checkout", "-q", "main")
    ledger = _Ledger()

    wave = Wave()
    propose_branch(wave, repo, "gb/w-1", ["SA-580", "SA-581"], client=ledger)

    assert [(br, base) for br, base, _, _ in forge] == [
        ("gb/w-1--SA-580", "origin/main"),
        ("gb/w-1--SA-581", "origin/main"),
    ], wave.failures
    titles = [t for _, _, t, _ in forge]
    assert titles == ["SA-580: board filter", "SA-581: card badge"]
    for t in titles:
        assert sum(i in t for i in ("SA-580", "SA-581")) == 1, t
    bodies = [body for _, _, _, body in forge]
    assert _attested(bodies[0]) == ["SA-580"]
    assert _attested(bodies[1]) == ["SA-581"]
    assert "SA-581" not in bodies[0]
    assert "SA-580" not in bodies[1]
    # A's prefix keeps its own commit. B is replayed onto the base, so A's file is not in it.
    assert _git(repo, "rev-parse", "gb/w-1--SA-580").strip() == a
    assert _git(repo, "rev-parse", "gb/w-1--SA-581").strip() != b
    assert _git(repo, "diff", "--name-only", "main", "gb/w-1--SA-580").split() == ["web/a.tsx"]
    assert _git(repo, "diff", "--name-only", "main", "gb/w-1--SA-581").split() == ["web/b.tsx"]
    # The receipt lands on its own item and names its own PR.
    urls = {(o["item_id"], o["url"]) for o in ledger.observations if o["kind"] == "pr_opened"}
    assert urls == {("SA-580", "https://x/pull/1"), ("SA-581", "https://x/pull/2")}
    assert not wave.failures, wave.failures


def test_interleaved_commits_that_apply_alone_become_one_pr_each(published, forge):
    """A later item's commit in between does not glue the histories together when each
    patch applies on the base by itself."""
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-5")
    _commit(repo, "a.py", "SA-580: one")
    _commit(repo, "b.py", "SA-581: two")
    _commit(repo, "c.py", "SA-580: three")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-5", ["SA-580", "SA-581"], client=_Ledger())

    assert [(br, base) for br, base, _, _ in forge] == [
        ("gb/w-5--SA-580", "origin/main"),
        ("gb/w-5--SA-581", "origin/main"),
    ], wave.failures
    assert set(_git(repo, "diff", "--name-only", "main", "gb/w-5--SA-580").split()) == {"a.py", "c.py"}
    assert _git(repo, "diff", "--name-only", "main", "gb/w-5--SA-581").split() == ["b.py"]
    assert _attested(forge[0][3]) == ["SA-580"]
    assert _attested(forge[1][3]) == ["SA-581"]


def test_a_commit_that_depends_on_another_items_commit_is_not_proposed(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-2")
    _commit(repo, "web/a.tsx", "SA-580: board")
    path = repo / "web/a.tsx"
    path.write_text(path.read_text(encoding="utf-8") + "badge\n", encoding="utf-8")
    _git(repo, "add", "web/a.tsx")
    _git(repo, "commit", "-qm", "SA-581: badge")
    b = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-2", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    assert b[:12] in wave.unproposed["gb/w-2"]
    assert "depends" in wave.unproposed["gb/w-2"]
    assert any("not proposed" in f for f in wave.failures)


def test_a_commit_naming_no_item_is_not_proposed(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-6")
    _commit(repo, "web/a.tsx", "SA-580: board filter")
    blank = _commit(repo, "web/a2.tsx", "tidy the filter")
    _commit(repo, "web/b.tsx", "SA-581: card badge")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-6", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    assert blank[:12] in wave.unproposed["gb/w-6"]
    assert "names no item" in wave.unproposed["gb/w-6"]


def test_a_commit_naming_two_items_is_not_proposed(published, forge):
    repo = published
    _git(repo, "checkout", "-q", "-b", "gb/w-3")
    both = _commit(repo, "a.py", "SA-580, SA-581: both at once")
    _git(repo, "checkout", "-q", "main")

    wave = Wave()
    propose_branch(wave, repo, "gb/w-3", ["SA-580", "SA-581"], client=_Ledger())

    assert forge == []
    assert both[:12] in wave.unproposed["gb/w-3"]
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

    assert [(br, base, title) for br, base, title, _ in forge] == [
        ("gb/w-4", "origin/main", "SA-580: one")]
    assert "Attests:" not in forge[0][3]


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
    notes = {o["item_id"]: o["branch"] for o in ledger.observations if o["kind"] == "salvage"}
    assert notes["GRPH-1"] == "gb/w-8"
    assert notes["GRPH-2"] == "gb/w-8--GRPH-2"
    assert wave.published["gb/w-8--GRPH-2"].ok, "the sibling is pushed so it can be found"
