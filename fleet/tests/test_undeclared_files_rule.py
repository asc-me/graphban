"""GRPH-1425: `build.undeclared_files` — an undeclared file holds the PR (`hold`) or only warns.

Sabotage: make `warn` skip `_warn_undeclared`'s item note and the warn-note test fails; make
`warn` also skip the merge-time check (`_outside_files` in `_merge`) and the last test merges.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gbfleet import propose as propose_mod
from gbfleet import worktree as wt_mod
from gbfleet.client import Graphban
from gbfleet.supervisor import Wave, propose_branch

from tests.test_propose import REVIEWED, _Forge, gh  # noqa: F401

UNDECLARED = "web/src/App.tsx"
DECLARED = ["backend/app/services/collision.py"]


@pytest.fixture
def no_forge(monkeypatch):
    opened = []
    monkeypatch.setattr(wt_mod, "remote_for", lambda repo: "origin")
    monkeypatch.setattr(wt_mod, "default_ref", lambda repo, remote: "origin/main")
    monkeypatch.setattr(propose_mod, "subject", lambda repo, branch, base: "GRPH-1: real work")
    monkeypatch.setattr(propose_mod, "propose",
                        lambda *a, **k: opened.append(a) or propose_mod.Proposed(
                            branch=a[1], url="https://x/pull/1", ok=True))
    return opened


def _client(mode: str | None):
    client = MagicMock(spec=Graphban)
    toggles = [] if mode is None else [
        {"rule_id": "build.undeclared_files", "params": {"value": {"mode": mode}}}]
    client.call = MagicMock(side_effect=lambda tool, **kw: {"toggles": toggles})
    return client


def _wave() -> Wave:
    wave = Wave()
    wave.undeclared["wave/1"] = [UNDECLARED]
    return wave


def test_unset_rule_holds_the_branch_exactly_as_before(no_forge):
    wave, client = _wave(), _client(None)
    propose_branch(wave, Path("."), "wave/1", ["GRPH-1"], client=client)
    assert no_forge == []
    got = wave.proposed["wave/1"]
    assert got.skipped and not got.url and UNDECLARED in got.reason
    assert not any(c.kwargs.get("kind") == "undeclared_warned"
                   for c in client.post_observation.call_args_list)


def test_warn_opens_the_pr_and_the_item_carries_a_note_naming_the_file(no_forge):
    wave, client = _wave(), _client("warn")
    propose_branch(wave, Path("."), "wave/1", ["GRPH-1"], client=client)
    assert len(no_forge) == 1 and wave.proposed["wave/1"].url
    notes = [c.kwargs for c in client.post_observation.call_args_list
             if c.kwargs.get("kind") == "undeclared_warned"]
    assert len(notes) == 1
    assert notes[0]["item_id"] == "GRPH-1" and UNDECLARED in notes[0]["reason"]


def test_warn_still_lets_the_merge_check_refuse_the_uncovered_file(gh):  # noqa: F811
    """The PR `warn` opened carries an undeclared file; no signed-off item covers it."""
    gh(_Forge(draft=False, files=DECLARED + [UNDECLARED]))
    got = propose_mod.merge(Path("."), "GRPH-A", "gb/w-1", reviewed=REVIEWED,
                            green={REVIEWED}, paths=DECLARED, covering=())
    assert not got.ok and not got.commit
    assert UNDECLARED in got.reason
