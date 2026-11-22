"""`_dependencies_not_done` counts only an item's own dependencies (GRPH-1866).

A depends on B (done); C depends on A (backlog). `related_work` returns both
directions with no direction field, so C used to count as A's dependency and A was
held on work that had never been built.

Sabotage: restore the `related_work` read → the first test fails.
"""
from __future__ import annotations

from gbfleet import until


class Server:
    def __init__(self, b_status="done"):
        self.status = {"A": "next", "B": b_status, "C": "backlog"}
        self.links = [{"type": "dependency", "a": "A", "b": "B"},
                      {"type": "dependency", "a": "C", "b": "A"}]

    def call(self, tool, **kw):
        if tool == "get_item_details":
            i = kw["id"]
            links = [lk for lk in self.links if i in (lk["a"], lk["b"])]
            return {"id": i, "status": self.status[i], "links": links}
        if tool == "related_work":
            return {"results": [
                {"id": "B", "status": self.status["B"], "link_types": ["dependency"]},
                {"id": "C", "status": "backlog", "link_types": ["dependency"]}]}
        raise AssertionError(tool)


def test_dependents_are_not_dependencies():
    assert until._dependencies_not_done(Server("done"), "A") == []


def test_reopened_dependency_holds_on_it_alone():
    rows = until._dependencies_not_done(Server("review"), "A")
    assert [r["id"] for r in rows] == ["B"]
