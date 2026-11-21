"""A branch is proposed for the item its commits name, not the roster's last holding (GRPH-1657).

Seen on gb/audit-w2-1 and gb/audit-w3-1: a bound child built GRPH-1562, then `claim_next`
handed it GRPH-933, which it released unbuilt. At reap the roster's `held_items` was
[GRPH-933]. `_proposal_items` returned `held_items` whenever fewer than two items were named
by commits, so the branch was proposed for GRPH-933 and withheld: "proposed for GRPH-933 but
carries commits for GRPH-1562". The PRs were opened by hand.

Sabotage: restore `if len(named) > 1` in `_proposal_items` and the first test withholds the
branch.
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
from gbfleet.supervisor import Wave

BUILT, HELD = "GRPH-1562", "GRPH-933"


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
    allowed = frozenset({"search_items", "get_item_details", "fleet_status",
                         "propose_allocation"})

    def __init__(self):
        self.observations: list[dict] = []

    def call(self, tool, /, **kw):
        if tool == "search_items":
            return {"results": []}
        if tool == "get_item_details":
            return {"id": kw.get("id"), "evidence": [], "branch": ""}
        return {}

    def post_attempt(self, **kw):
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

    def propose(repo, branch, base, *, title, body, **_kw):
        opened.append((branch, base, title, body))
        return propose_mod.Proposed(branch=branch, url=f"https://x/pull/{len(opened)}", ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


def _child(tree, tmp_path: Path) -> Child:
    """Bound to BUILT; the roster's last reading holds HELD, which was never committed for."""
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", assigned={"item": BUILT, "state": "claimed"},
                  held_items=[HELD])
    child.ever_held = [BUILT, HELD]
    return child


def test_a_branch_is_proposed_for_the_one_item_its_commits_name(
        git_repo: Path, origin: Path, forge, tmp_path: Path):
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A1")
    _commit(tree.path, "audit.py", f"{BUILT}: the audit fix")
    child = _child(tree, tmp_path)
    ledger = _Ledger()
    wave = Wave()

    assert sup._proposal_items(tree, child) == [BUILT]
    sup._propose(wave, tree, client=ledger, child=child)

    assert tree.branch not in wave.unproposed, wave.unproposed
    assert len(forge) == 1, wave.failures
    _branch, _base, title, _body = forge[0]
    assert BUILT in title and HELD not in title, title
    withheld = [o for o in ledger.observations if o["kind"] == "withheld"]
    assert withheld == [], withheld
    assert not any(HELD in f for f in wave.failures), wave.failures
    opened = [o["item_id"] for o in ledger.observations if o["kind"] == "pr_opened"]
    assert opened == [BUILT], ledger.observations


def test_a_branch_with_no_attributed_commits_keeps_the_held_items(
        git_repo: Path, origin: Path, tmp_path: Path):
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A2")
    _commit(tree.path, "notes.py", "tidy the notes")
    child = _child(tree, tmp_path)

    assert sup._proposal_items(tree, child) == [HELD]
