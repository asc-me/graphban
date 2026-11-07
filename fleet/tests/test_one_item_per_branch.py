"""A worker's second item does not ride on its first item's branch (GRPH-1407).

Seen on wave p59h: a bound child committed GRPH-1359 on `gb/p59h-1`, moved it to review, then
claimed GRPH-1399 and committed that on the SAME branch. GRPH-1359's reviewer read a foreign
commit and bounced it. At reap the roster's last reading held only GRPH-1399, so the branch
was proposed as GRPH-1399's: PR #1388, titled for one item, carrying both, merged as one.

What these pin:

* The shared branch on the remote stops at the first item's commits. The second item's
  commits are not pushed under the first item's name, where its reviewer reads.
* At reap the branch is proposed for every item a commit on it names, so it splits into one
  branch and one PR per item, each carrying only its own commit and each receipt naming its
  own branch.
* A one-item proposal of a branch carrying another item's commits is refused, and the refusal
  names both items and the foreign commit.

Sabotage: push the branch tip in `_publish_in_review` instead of `_first_item_tip`'s commit
(the second claim reuses the first branch) and the first test fails. Make `_proposal_items`
return `held_items` again and the reap test fails. Drop `_carries_another_item` from
`propose_branch` and the refusal test fails.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet import worktree as wt
from gbfleet.spawn import Child
from gbfleet.supervisor import Wave, propose_branch

A, B = "GRPH-1359", "GRPH-1399"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(tree: Path, path: str, subject: str) -> str:
    (tree / path).write_text(subject + "\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", subject)
    return _git(tree, "rev-parse", "HEAD")


class _Live:
    pid = 4242
    returncode = None

    def poll(self):
        return None


class _Ledger:
    """Answers the review queue, item reads and the two POST routes, and records the posts."""

    allowed = frozenset({"search_items", "get_item_details", "fleet_status",
                         "propose_allocation"})

    def __init__(self):
        self.review: list[str] = []
        self.attempts: list[dict] = []
        self.observations: list[dict] = []

    def call(self, tool, /, **kw):
        if tool == "search_items":
            if kw.get("status") not in (None, "review"):
                return {"results": []}
            return {"results": [{"id": i, "status": "review"} for i in self.review]}
        if tool == "get_item_details":
            return {"id": kw.get("id"), "evidence": [], "branch": ""}
        return {}

    def post_attempt(self, **kw):
        self.attempts.append(kw)
        return {"id": "at_1"}

    def post_observation(self, **kw):
        self.observations.append(kw)
        return {"recorded": True}


@pytest.fixture
def origin(git_repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(git_repo, "remote", "add", "origin", str(bare))
    _git(git_repo, "push", "-q", "origin", "main:main")
    _git(git_repo, "fetch", "-q", "origin")
    return bare


@pytest.fixture
def forge(monkeypatch):
    opened: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(wt, "default_ref", lambda repo, remote: "origin/main")

    def propose(repo, branch, base, *, title, body):
        opened.append((branch, base, title, body))
        return propose_mod.Proposed(branch=branch, url=f"https://x/pull/{len(opened)}", ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


def _remote_files(bare: Path, branch: str, base: str = "main") -> list[str]:
    return _git(bare, "diff", "--name-only", base, branch).split()


def test_a_second_item_built_on_the_branch_is_not_pushed_under_the_first(
        git_repo: Path, origin: Path, forge, tmp_path: Path):
    """The fake child: claims A, commits, moves A to review, claims B, commits, moves B to
    review, and is reaped. A and B end on different branches, each carrying only its own
    commit, and each item's receipt names its own branch."""
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A1")
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", assigned={"item": A, "state": "claimed"},
                  held_items=[A])
    child.ever_held = [A]
    ledger = _Ledger()
    wave = Wave()

    # A: built, committed, moved to review — published for its reviewer.
    a_sha = _commit(tree.path, "a.py", f"{A}: the first item")
    ledger.review = [A]
    sup._publish_in_review(wave, [child], ledger)
    assert _git(origin, "rev-parse", tree.branch) == a_sha

    # B: claimed next and committed on the same worktree, then moved to review.
    child.held_items = [B]
    child.ever_held = [A, B]
    b_sha = _commit(tree.path, "b.py", f"{B}: the second item")
    ledger.review = [A, B]
    sup._publish_in_review(wave, [child], ledger)

    # A's branch, the one A's reviewer reads, still carries A alone.
    assert _git(origin, "rev-parse", tree.branch) == a_sha, (
        "the second item's commit was pushed onto the first item's branch")
    assert _remote_files(origin, tree.branch) == ["a.py"]

    # Reap: one branch and one PR per item, each cut from the base.
    sup._propose(wave, tree, client=ledger, child=child)
    assert [br for br, _, _, _ in forge] == [wt.item_branch(tree.branch, A),
                                             wt.item_branch(tree.branch, B)], wave.failures
    for name, item, other in ((wt.item_branch(tree.branch, A), "a.py", A),
                              (wt.item_branch(tree.branch, B), "b.py", B)):
        assert _git(git_repo, "diff", "--name-only", "main", name).split() == [item]
        log = _git(git_repo, "log", "--format=%s", f"main..{name}").splitlines()
        assert len(log) == 1 and other in log[0], log
    assert _git(git_repo, "rev-parse", wt.item_branch(tree.branch, A)) == a_sha
    assert _git(git_repo, "rev-parse", wt.item_branch(tree.branch, B)) != b_sha
    titles = [t for _, _, t, _ in forge]
    assert A in titles[0] and B not in titles[0]
    assert B in titles[1] and A not in titles[1]
    receipts = {(o["item_id"], o["branch"]) for o in ledger.observations
                if o["kind"] == "pr_opened"}
    assert receipts == {(A, wt.item_branch(tree.branch, A)),
                        (B, wt.item_branch(tree.branch, B))}


def test_a_later_item_alone_in_review_is_not_reported_published(
        git_repo: Path, origin: Path, tmp_path: Path):
    """When only the second item is awaiting review, its commits are not on the shared ref,
    so `branch_published` would hand its reviewer the first item's diff."""
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A2")
    _commit(tree.path, "a.py", f"{A}: the first item")
    _commit(tree.path, "b.py", f"{B}: the second item")
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", assigned={"item": A}, held_items=[B])
    child.ever_held = [A, B]
    ledger = _Ledger()
    ledger.review = [B]
    wave = Wave()

    sup._publish_in_review(wave, [child], ledger)

    assert _remote_files(origin, tree.branch) == ["a.py"]
    assert not [p for p in ledger.attempts if p.get("branch_published")]
    assert wave.review_unreadable[tree.branch] == [B]
    assert any(A in f and B in f for f in wave.failures), wave.failures


def test_a_branch_carrying_two_items_is_refused_a_one_item_pr(git_repo: Path, origin: Path,
                                                              forge):
    """The p59h reap: the supervisor knew only B, and the branch carried A's commit too."""
    _git(git_repo, "checkout", "-q", "-b", "gb/w-1")
    (git_repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-qm", f"{A}: the first item")
    a_sha = _git(git_repo, "rev-parse", "HEAD")
    (git_repo / "b.py").write_text("b\n", encoding="utf-8")
    _git(git_repo, "add", "b.py")
    _git(git_repo, "commit", "-qm", f"{B}: the second item")
    _git(git_repo, "checkout", "-q", "main")
    ledger = _Ledger()
    wave = Wave()

    propose_branch(wave, git_repo, "gb/w-1", [B], client=ledger)

    assert forge == [], "a branch carrying two items was proposed under one item's title"
    reason = wave.unproposed["gb/w-1"]
    assert A in reason and B in reason and a_sha[:12] in reason, reason
    withheld = [o for o in ledger.observations if o["kind"] == "withheld"]
    assert [o["item_id"] for o in withheld] == [B]


def test_a_branch_of_one_item_is_still_proposed(git_repo: Path, origin: Path, forge):
    """Mentioning a PRD or another tracker's ticket in a subject is not another item."""
    _git(git_repo, "checkout", "-q", "-b", "gb/w-2")
    (git_repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-qm", f"{A}: per PRD-39 D-h and JIRA-12")
    _git(git_repo, "checkout", "-q", "main")
    wave = Wave()

    propose_branch(wave, git_repo, "gb/w-2", [A], client=_Ledger())

    assert [br for br, _, _, _ in forge] == ["gb/w-2"], wave.failures


def test_attribution_reads_only_the_projects_own_ids():
    assert wt.named_items("GRPH-1: fix (GRPH-2) per PRD-39", {"GRPH"}) == ["GRPH-1", "GRPH-2"]
    assert wt.named_items("GRPH-P59 framing", {"GRPH"}) == []
    assert wt.named_items("XGRPH-1 is not one", {"GRPH"}) == []
