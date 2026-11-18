"""GRPH-1697 — `gbfleet until --line`: resolution, review split by scope, unready."""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import until
from gbfleet.until import ConfigError, run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

LINE = {"id": "GRPH-L1", "key": "GRPH-L1"}


def _go(git_repo, tmp_path, scripts, state, **kw):
    workspace = tmp_path / "ws"
    calls: list = []
    line = kw.pop("line", None)
    prd = kw.pop("prd", None)
    planner, supervisor = _clients(workspace, calls=calls, **kw)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        line=line, prd=prd,
    )
    return result, calls


def test_prd_with_line_is_refused(git_repo, tmp_path, scripts, state):
    result, _ = _go(git_repo, tmp_path, scripts, state, line="", prd="GRPH-P1",
                    maintenance_line=LINE)
    assert result.reason == "config" and result.exit == 2


def test_no_line_names_create_line(git_repo, tmp_path, scripts, state):
    result, _ = _go(git_repo, tmp_path, scripts, state, line="")
    assert result.exit == 2 and "create_line" in result.detail


def test_a_prd_id_is_refused_with_use_prd(git_repo, tmp_path, scripts, state):
    result, _ = _go(git_repo, tmp_path, scripts, state, line="GRPH-P86", maintenance_line=LINE)
    assert result.exit == 2 and "use --prd" in result.detail


def test_line_resolution_never_calls_get_prd(git_repo, tmp_path, scripts, state):
    result, calls = _go(git_repo, tmp_path, scripts, state, line="", maintenance_line=LINE)
    assert result.reason == "idle"
    assert "get_prd" not in calls
    assert result.as_json()["unready"] == []


def test_only_unready_items_finish_unready(git_repo, tmp_path, scripts, state):
    backlog = [{"id": "GRPH-9", "status": "next", "line_id": "GRPH-L1",
                "description": "no clause", "touchpoints": []}]
    result, _ = _go(git_repo, tmp_path, scripts, state, line="", maintenance_line=LINE,
                    backlog=backlog)
    out = result.as_json()
    assert (out["ok"], out["reason"], out["exit"]) == (False, "unready", 0)
    assert out["unready"] == [{"id": "GRPH-9", "missing": ["acceptance", "touchpoints"]}]


def test_a_ready_line_item_is_not_unready(git_repo, tmp_path, scripts, state):
    backlog = [{"id": "GRPH-9", "status": "next", "line_id": "GRPH-L1",
                "description": "x\n\n## Acceptance\n- it works\n", "touchpoints": ["a.py"]}]
    result, _ = _go(git_repo, tmp_path, scripts, state, line="", maintenance_line=LINE,
                    backlog=backlog)
    assert result.as_json()["unready"] == []


def test_split_and_widen():
    rows = [{"id": "a", "prd_id": "P1"}, {"id": "b", "prd_id": "P2"},
            {"id": "c", "line_id": "L"}, {"id": "d"}, {"id": "e", "prd_id": "P2", "line_id": "L"}]
    inside, outside = until._split_by_prd(rows, "P1")
    assert [r["id"] for r in inside] == ["a"]
    assert [r["id"] for r in until._widen(outside, "P1", None)] == ["b"]
    inside, outside = until._split_by_prd(rows, None, "L")
    assert [r["id"] for r in inside] == ["c", "e"]
    assert until._widen(outside, None, "L") == []


def test_staff_every_row_does_not_spawn_for_line_rows_on_a_prd_run(
        git_repo, tmp_path, scripts, state):
    review = [{"id": "GRPH-5", "line_id": "GRPH-L1"}, {"id": "GRPH-6"}]
    result, _ = _go(git_repo, tmp_path, scripts, state, prd="GRPH-P1", staff_every_row=True,
                    review=review)
    out = result.as_json()
    assert out["reason"] == "idle" and out["spawned"] == 0
    assert sorted(out["review_outside_prd"]) == ["GRPH-5", "GRPH-6"]
