"""GRPH-1708 — a builder that blocks on missing local CI is not a human wait.

Sabotage: drop the `builder_ci_pending` branch in `_loop` and the pending loop test idles;
drop `_apply_builder_ci_blockers` from the wait read and the red test stays `idle-with-waits`.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import until
from gbfleet.until import _apply_builder_ci_blockers, _builder_ci_misblocked_rows

HEAD = "abcdef1234567890"
ROW = {
    "id": "GRPH-1681",
    "status": "blocked",
    "branch": "gb/p86-1",
    "built_by": "GRPH-A1",
    "blocker": "cannot reach review: needs CI postgres",
    "prd_id": "PRD-86",
}


class _Planner:
    def __init__(self, rows: dict[str, list]):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        if tool == "search_items":
            status = args.get("status")
            return {"results": list(self.rows.get(status, []))}
        if tool == "update_item":
            return {}
        return {}


def test_misblocked_rows_need_built_by_blocker_and_branch():
    planner = _Planner({
        "blocked": [ROW],
        "next": [],
        "backlog": [],
    })
    assert _builder_ci_misblocked_rows(planner, "PRD-86", None) == [ROW]
    assert _builder_ci_misblocked_rows(planner, "PRD-99", None) == []
    bare = {**ROW, "built_by": ""}
    planner = _Planner({"blocked": [bare], "next": [], "backlog": []})
    assert _builder_ci_misblocked_rows(planner, "PRD-86", None) == []


def test_red_head_reopens_and_names_the_job(monkeypatch):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "backend-postgres"))
    monkeypatch.setattr(until, "failed_test_summary", lambda *a: "")
    planner = _Planner({"blocked": [ROW], "next": [], "backlog": []})
    logged: set = set()
    waits, pending, reopened = _apply_builder_ci_blockers(
        planner, [ROW], Path("."), ["GRPH-1681"], logged, agent_id="GRPH-P1")
    assert reopened is True
    assert pending is False
    assert "GRPH-1681" not in waits
    assert any(c[0] == "update_item" and c[1].get("status") == "next"
               for c in planner.calls)


def test_pending_is_named_and_not_a_bare_wait_id(monkeypatch):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("pending", HEAD, ""))
    planner = _Planner({})
    waits, pending, _ = _apply_builder_ci_blockers(
        planner, [ROW], Path("."), ["GRPH-1681"], set())
    assert pending is True
    assert waits == [f"GRPH-1681: CI pending at {HEAD[:12]}"]
    assert "GRPH-1681" not in waits


def test_sabotage_skipping_reopen_names_ci_red_instead_of_redelegating(monkeypatch):
    """Sabotage: if `_block_ci_red` always refuses, the wait line must name the failing job."""
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "backend"))
    monkeypatch.setattr(until, "_block_ci_red", lambda *a, **k: False)
    waits, pending, reopened = _apply_builder_ci_blockers(
        _Planner({}), [ROW], Path("."), ["GRPH-1681"], set())
    assert reopened is False
    assert waits == ["GRPH-1681: CI red (backend)"]