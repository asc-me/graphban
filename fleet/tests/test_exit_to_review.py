"""A child that exits with a committed, CI-green branch leaves its item in review (GRPH-1391).

Sabotage: make `_exit_to_review` ignore `ci_green` and the red/absent-CI tests fail; drop the
`skip=` at the `_release_held_items` call and the green test sees the item released as well.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from gbfleet import supervisor as sup
from gbfleet.client import Graphban
from gbfleet.seat import BOUND_INSTRUCTION, BOUND_ONLY_INSTRUCTION, CI_RECEIPT, INSTRUCTION


def _child(items):
    return SimpleNamespace(held_items=list(items), agent_id="GRPH-A1", branch="gb/x",
                           base="abc", worktree=Path("/tmp/x"))


def _client(allowed=("update_item", "get_item_details", "release_item")):
    client = MagicMock(spec=Graphban)
    client.allowed = frozenset(allowed)
    client.call = MagicMock(side_effect=lambda tool, **kw: (
        {"claimed_by": "GRPH-A1"} if tool == "get_item_details" else {}))
    return client


def _calls(client, tool):
    return [c.kwargs for c in client.call.call_args_list if c.args[0] == tool]


def _patch(monkeypatch, *, commits=True, green=True):
    monkeypatch.setattr(sup.wt_mod, "commits_beyond_base", lambda *a: commits)
    monkeypatch.setattr(sup, "_branch_head", lambda *a: "deadbeefcafe1234")
    monkeypatch.setattr(sup.propose_mod, "ci_green", lambda *a: green)


def _tree():
    return SimpleNamespace(repo=Path("/tmp/repo"))


def test_committed_ci_green_branch_goes_to_review_not_next(monkeypatch):
    _patch(monkeypatch)
    wave, client, child = sup.Wave(), _client(), _child(["GRPH-1"])
    moved = sup._exit_to_review(wave, child, _tree(), client)
    sup._release_held_items(wave, child, client, skip=moved)
    updates = _calls(client, "update_item")
    assert moved == {"GRPH-1"}
    assert updates[0]["status"] == "review"
    assert "CI green" in updates[0]["evidence"][0]["detail"]
    assert _calls(client, "release_item") == []


def test_red_or_absent_ci_still_releases(monkeypatch):
    _patch(monkeypatch, green=False)
    wave, client, child = sup.Wave(), _client(), _child(["GRPH-1"])
    moved = sup._exit_to_review(wave, child, _tree(), client)
    sup._release_held_items(wave, child, client, skip=moved)
    assert moved == set()
    assert _calls(client, "update_item") == []
    assert [c["id"] for c in _calls(client, "release_item")] == ["GRPH-1"]


def test_no_commits_beyond_base_still_releases(monkeypatch):
    _patch(monkeypatch, commits=False)
    client = _client()
    assert sup._exit_to_review(sup.Wave(), _child(["GRPH-1"]), _tree(), client) == set()
    assert _calls(client, "update_item") == []


def test_brief_says_ci_is_the_full_suite_receipt():
    assert "CI is the full-suite receipt" in CI_RECEIPT
    for tmpl in (INSTRUCTION, BOUND_INSTRUCTION, BOUND_ONLY_INSTRUCTION):
        assert CI_RECEIPT in tmpl
