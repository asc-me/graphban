"""Salvaged work is published and named on the item it belongs to (GRPH-830, GRPH-926).

Adopting a stranded worktree already worked, and the field report says so — it reaped a tree
left by an OOM kill after confirming the only uncommitted file was the seat. The case that
matters behaved differently.

A wave killed mid-build was recovered as a local commit `WIP: salvaged by gbfleet`: 614
insertions across four files that were exactly one item's touchpoints. Then nothing. The commit
was local-only, never pushed, and nothing linked it to the item. That item was re-delegated
minutes later, branched from `main`, and rebuilt all 614 lines. The work was recovered and lost
in the same move, and only somebody reading local refs could have known.

Push is still the step that matters. A draft PR is not: those salvage subjects were landing
as mergeable `WIP: salvaged by gbfleet` titles. The item gets a note naming branch + commit.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from gbfleet import adopt
from gbfleet import supervisor as sup
from gbfleet.adopt import Snapshot, save
from gbfleet.supervisor import Wave
from gbfleet.worktree import create


def _dead_child_with_work(git_repo: Path, workspace: Path, state: Path,
                          items: list[str] | None = None, content=True):
    """A record for a process that is gone, whose worktree has uncommitted work in it."""
    workspace.mkdir(exist_ok=True)
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    if content:
        (tree.path / "feature.py").write_text("print('614 lines, morally')\n", encoding="utf-8")
    save(adopt.children_path(git_repo, state), [Snapshot(
        # A pid that is certainly not running: `classify` reports `gone`, which is the
        # crashed-supervisor case rather than the killed-child one.
        pid=2 ** 22, start_token="tok",
        worktree=str(tree.path), branch=tree.branch, adapter="fake",
        slot="1", base=tree.base, started_wall=time.time(),
        held_items=list(items or []),
    )])
    return tree


def test_salvaged_work_is_reported_with_the_item_it_belongs_to(
    git_repo: Path, tmp_path: Path, state: Path
):
    """THE ONE THAT MATTERS. Recovery is not the end of the job — knowing whose work it was is.

    Sabotage: drop `held_items` from `_as_dict` and the branch still salvages, still pushes,
    and lands with nobody to hand the receipt to."""
    tree = _dead_child_with_work(git_repo, tmp_path / "ws", state, items=["SA-412"])

    got = adopt.recover(git_repo, tmp_path / "ws", state)

    assert [s.branch for s in got.salvaged] == [tree.branch]
    assert got.salvaged[0].items == ["SA-412"]


def test_a_tree_holding_only_the_credential_is_not_published(
    git_repo: Path, tmp_path: Path, state: Path
):
    """THE CONTROL, and the reason `Disposition` has four values. `ONLY_CREDENTIAL` means the
    single uncommitted file was the seat — nothing to publish, and a pushed empty branch per
    dead child would make every crash look like work."""
    _dead_child_with_work(git_repo, tmp_path / "ws", state, content=False)

    got = adopt.recover(git_repo, tmp_path / "ws", state)

    assert got.salvaged == []


def test_recover_still_unpacks_as_three(git_repo: Path, tmp_path: Path, state: Path):
    """Every caller in this package unpacks three, and two more do in tests. Widening the
    arity reads fine and breaks the crash path — the one path that only runs when something
    has already gone wrong."""
    _dead_child_with_work(git_repo, tmp_path / "ws", state)

    leftover, occupied, notes = adopt.recover(git_repo, tmp_path / "ws", state)

    assert leftover == [] and occupied and notes


def test_a_record_from_an_older_supervisor_still_salvages(
    git_repo: Path, tmp_path: Path, state: Path
):
    """`held_items` did not exist before this. A takeover that refused to read the old shape
    would strand exactly the trees it is there to save."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    (tree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    path = adopt.children_path(git_repo, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generation": adopt.GENERATION, "children": [{
        "pid": 2 ** 22, "start_token": "tok", "worktree": str(tree.path),
        "branch": tree.branch, "adapter": "fake", "slot": "1", "base": tree.base,
    }]}), encoding="utf-8")

    got = adopt.recover(git_repo, workspace, state)

    assert [s.branch for s in got.salvaged] == [tree.branch]
    assert got.salvaged[0].items == [], "invented an item the record never carried"


def test_publishing_pushes_and_does_not_propose_a_pr(monkeypatch, tmp_path: Path):
    """THE CALL (GRPH-926). Salvage is a commit on a pushed branch, not a mergeable PR.

    Sabotage: restore `propose_branch(...)` inside `publish_salvaged` and this fails.
    A PR for a salvage subject is how `WIP: salvaged by gbfleet` drafts got merged.
    """
    import inspect

    calls: list[tuple] = []

    class _Pushed:
        ok, skipped, reason = True, False, ""

    monkeypatch.setattr(sup.wt_mod, "push_branch",
                        lambda repo, branch, base: calls.append(("push", branch)) or _Pushed())
    monkeypatch.setattr(sup, "propose_branch",
                        lambda *a, **k: calls.append(("propose",)))
    monkeypatch.setattr(sup, "_branch_head", lambda repo, branch: "abc123def456")

    class _Client:
        def __init__(self):
            self.calls = []
        def call(self, tool, **kwargs):
            self.calls.append((tool, kwargs))
            return {}

    wave = Wave()
    client = _Client()
    sup.publish_salvaged(wave, tmp_path, [adopt.Salvaged("gb/w-1", "main", ["SA-412"])],
                         client=client)

    assert calls == [("push", "gb/w-1")], calls
    assert "propose_branch" not in inspect.getsource(sup.publish_salvaged)
    assert client.calls, "the item was not named"
    tool, kwargs = client.calls[0]
    assert tool == "update_item" and kwargs["id"] == "SA-412"
    ev = kwargs["evidence"][0]
    assert ev["kind"] == "note"
    assert "gb/w-1" in ev["detail"] and "not proposed as a PR" in ev["detail"]
    assert "/pull/" not in ev["detail"]


def test_a_failed_push_is_reported_and_never_fatal(monkeypatch, tmp_path: Path):
    """This runs at the very start of a wave, on the crash path. A takeover that refused to
    proceed because a push failed would strand the next wave too."""
    def _boom(repo, branch, base):
        raise RuntimeError("no remote, no network, no luck")

    monkeypatch.setattr(sup.wt_mod, "push_branch", _boom)
    wave = Wave()

    sup.publish_salvaged(wave, tmp_path, [adopt.Salvaged("gb/w-1", "main", ["SA-412"])],
                         client=None)

    assert any("salvaged but not published" in f for f in wave.failures)
