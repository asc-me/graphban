"""A drain pass clears a blocker that names only done items (GRPH-1530).

The server clears one when the dependency reaches done. This pass is what sees
a blocker whose items were already done, and what leaves a person or a decision
alone. A client that cannot write does not pretend it checked.
"""
import ast
import inspect

from gbfleet.supervisor import (
    blocker_left_alone, clear_stale_blockers, watch_tick,
)


class _Client:
    def __init__(self, rows, details, *, allowed=None):
        self.allowed = allowed or frozenset({
            "search_items", "update_item", "get_item_details"})
        self.rows = rows
        self.details = details
        self.updates = []

    def call(self, tool, **kwargs):
        if tool == "search_items":
            status = kwargs["status"]
            matched = [row for row in self.rows if row.get("status") == status]
            return {"results": matched, "has_more": False, "total": len(matched)}
        if tool == "get_item_details":
            return self.details[kwargs["id"]]
        if tool == "update_item":
            self.updates.append(kwargs)
            return {}
        raise AssertionError(tool)


def test_a_blocker_that_names_only_a_done_item_is_cleared():
    client = _Client(
        rows=[{
            "id": "GRPH-1686",
            "status": "blocked",
            "blocker": "needs GRPH-1685's unready claim filter, not on main yet",
            "tags": [],
        }],
        details={"GRPH-1685": {"id": "GRPH-1685", "status": "done"}},
    )
    cleared = clear_stale_blockers(client)
    assert cleared == ["GRPH-1686"]
    assert client.updates == [{
        "id": "GRPH-1686",
        "blocker": "",
        "status": "next",
        "evidence": [{
            "kind": "note",
            "detail": "blocker cleared: GRPH-1685 done",
        }],
    }]


def test_a_next_item_keeps_its_status_when_the_blocker_clears():
    client = _Client(
        rows=[{
            "id": "GBC-78",
            "status": "next",
            "blocker": "needs GBC-77",
            "tags": [],
        }],
        details={"GBC-77": {"status": "done"}},
    )
    assert clear_stale_blockers(client) == ["GBC-78"]
    assert "status" not in client.updates[0]
    assert client.updates[0]["blocker"] == ""


def test_a_person_or_a_decision_or_an_open_dependency_is_left():
    client = _Client(
        rows=[
            {"id": "A", "status": "blocked", "blocker": "needs a person", "tags": []},
            {"id": "B", "status": "blocked", "blocker": "needs a decision", "tags": []},
            {"id": "C", "status": "next", "blocker": "needs GRPH-1", "tags": ["wait:decision"]},
            {"id": "D", "status": "blocked", "blocker": "needs GRPH-2", "tags": []},
            {"id": "E", "status": "next", "blocker": "needs the device-code API", "tags": []},
        ],
        details={
            "GRPH-1": {"status": "done"},
            "GRPH-2": {"status": "in_progress"},
        },
    )
    assert clear_stale_blockers(client) == []
    assert client.updates == []
    assert blocker_left_alone("needs a person", [])
    assert not blocker_left_alone("needs GRPH-1685's filter", [])


def test_a_client_that_cannot_write_does_not_read():
    client = _Client(rows=[], details={}, allowed=frozenset({"fleet_status"}))
    assert clear_stale_blockers(client) == []


def test_a_row_without_a_blocker_field_is_not_treated_as_clear():
    """A lean row omits `blocker`. That is 'not reported', not an empty blocker."""
    client = _Client(
        rows=[{"id": "GRPH-9", "status": "blocked", "tags": []}],
        details={},
    )
    assert clear_stale_blockers(client) == []


def test_watch_tick_calls_the_clear():
    """The sweep lives on the drain pass. Deleting the call leaves the function green."""
    tree = ast.parse(inspect.getsource(watch_tick))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    assert "clear_stale_blockers" in names
