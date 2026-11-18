"""GRPH-1708 — a builder that blocks on missing local CI is not a human wait.

Sabotage: drop the red branch in `_apply_builder_ci_blockers` and the red test neither
reopens the item nor names the job.

A typed `wait:*` row, a person's blocker, a dependency hold, and a row whose CI is green or
unknown are never touched (bounce of the first attempt: D11 waits must survive).
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


def test_unscoped_drain_matches_nothing():
    planner = _Planner({"blocked": [ROW], "next": []})
    assert _builder_ci_misblocked_rows(planner, None, None) == []
    assert planner.calls == []


def test_wait_tagged_row_is_never_a_builder_ci_row():
    tagged = {**ROW, "tags": ["wait:decision"], "blocker": "needs a person: decide CI pricing"}
    planner = _Planner({"blocked": [tagged], "next": []})
    assert _builder_ci_misblocked_rows(planner, "PRD-86", None) == []


def test_person_and_hold_blockers_are_not_builder_ci_rows():
    person = {**ROW, "blocker": "needs a person: decide pricing"}
    hold = {**ROW, "blocker": "waiting on GRPH-9 ci [gbfleet dependency hold]"}
    planner = _Planner({"blocked": [person, {**hold, "id": "GRPH-2"}], "next": []})
    assert _builder_ci_misblocked_rows(planner, "PRD-86", None) == []


def test_line_scope_matches_line_rows():
    on_line = {**ROW, "prd_id": None, "line_id": "GRPH-L1"}
    planner = _Planner({"blocked": [on_line], "next": []})
    assert _builder_ci_misblocked_rows(planner, None, "GRPH-L1") == [on_line]
    assert _builder_ci_misblocked_rows(planner, None, "GRPH-L2") == []


def test_green_or_unknown_ci_leaves_waits_untouched(monkeypatch):
    for state in ("green", "unknown"):
        monkeypatch.setattr(until, "ci_verdict", lambda *a, s=state: (s, HEAD, ""))
        planner = _Planner({})
        waits, pending, reopened = _apply_builder_ci_blockers(
            planner, [ROW], Path("."), ["GRPH-1681", "GRPH-7"], set())
        assert waits == ["GRPH-1681", "GRPH-7"]
        assert (pending, reopened) == (False, False)
        assert planner.calls == []


def test_wait_decision_row_survives_the_whole_read(monkeypatch):
    """The bounce repro: wait:decision + person blocker + green CI stays a wait."""
    tagged = {**ROW, "tags": ["wait:decision"], "blocker": "needs a person: decide pricing"}
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("green", HEAD, ""))
    planner = _Planner({"blocked": [tagged], "next": []})
    rows = _builder_ci_misblocked_rows(planner, "PRD-86", None)
    waits, _, _ = _apply_builder_ci_blockers(
        planner, rows, Path("."), ["GRPH-1681"], set())
    assert waits == ["GRPH-1681"]


def test_red_reopens_once_per_head(monkeypatch):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "backend-postgres"))
    monkeypatch.setattr(until, "failed_test_summary", lambda *a: "")
    planner = _Planner({})
    logged: set = set()
    _apply_builder_ci_blockers(planner, [ROW], Path("."), [], logged, agent_id="P")
    waits, _, reopened = _apply_builder_ci_blockers(
        planner, [ROW], Path("."), [], logged, agent_id="P")
    assert reopened is False
    assert waits == ["GRPH-1681: CI red (backend-postgres)"]
    assert sum(1 for c in planner.calls if c[0] == "update_item") == 1


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