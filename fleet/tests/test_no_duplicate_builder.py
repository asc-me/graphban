"""`until` does not delegate an item a live child of its own is still building (GRPH-1414).

The server lease on an item lapses while its child sits in a long tool call, and the item is
offered again. The child is alive; a second delegation puts two builders on one item.

Sabotage: remove the `_live_builder` check in `_delegate_next` and the first test fails.
"""
from __future__ import annotations

from types import SimpleNamespace

from gbfleet import observe, until

BRIEF = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}


class Planner:
    def __init__(self):
        self.delegated: list[str] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": [{"items": ["X"]}]}
        if tool == "get_item_details":
            return {"id": "X", "status": "next", "branch": "gb/w-1", "brief": BRIEF, "evidence": []}
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X"}
        raise AssertionError(tool)


def _child(running=True, branch="gb/other", held=()):
    return SimpleNamespace(running=running, branch=branch, held_items=list(held),
                           ever_held=[], assigned=None)


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(observe, "emit", lambda name, **kw: seen.append((name, kw)))
    return seen


def test_a_live_bound_child_blocks_a_second_delegate(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    child, planner, delegated = _child(), Planner(), set()

    seed, _c, _w = until._delegate_next(planner, "A", "w", delegated, None, repo=tmp_path,
                                        base="", live=[child], owners={"X": child})

    assert seed is None
    assert planner.delegated == []
    assert "X" not in delegated
    assert [n for n, _ in events if n == "redelegate_skipped_live_child"]


def test_a_live_child_on_the_items_branch_blocks_it(tmp_path, monkeypatch):
    _events(monkeypatch)
    planner = Planner()
    seed, *_ = until._delegate_next(planner, "A", "w", set(), None, repo=tmp_path, base="",
                                    live=[_child(branch="gb/w-1")], owners={})
    assert seed is None and planner.delegated == []


def test_a_live_child_that_held_it_blocks_it(tmp_path, monkeypatch):
    _events(monkeypatch)
    planner = Planner()
    seed, *_ = until._delegate_next(planner, "A", "w", set(), None, repo=tmp_path, base="",
                                    live=[_child(held=["X"])], owners={})
    assert seed is None and planner.delegated == []


def test_once_the_child_exits_the_item_is_delegated_again(tmp_path, monkeypatch):
    _events(monkeypatch)
    child, planner = _child(), Planner()
    until._delegate_next(planner, "A", "w", set(), None, repo=tmp_path, base="",
                         live=[child], owners={"X": child})
    child.running = False

    seed, code, _w = until._delegate_next(planner, "A", "w", set(), None, repo=tmp_path, base="",
                                          live=[child], owners={"X": child})

    assert seed == "X" and code == "WORKER-X"
    assert planner.delegated == ["X"]
