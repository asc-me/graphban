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


def _line_run(git_repo, tmp_path, scripts, state, **kw):
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(workspace, calls=calls, maintenance_line=LINE, **kw)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        line="", limits=until.Limits(max_workers=1),
    )
    return result, calls


def test_the_line_drain_staffs_its_own_review_row_with_a_line_seat(
        git_repo, tmp_path, scripts, state):
    """The line drain staffs a line row in review. Its review seat carries the line, and a PRD
    row in review is outside it and not staffed, even with `staff_every_row` on.

    Sabotage: drop the `line` branch in `_split_by_prd` and the line row is not inside."""
    mints: list[dict] = []
    review = [{"id": "GRPH-5", "status": "review", "claimed_by": "GRPH-A1",
               "review_claimed_by": "", "line_id": "GRPH-L1"},
              {"id": "GRPH-6", "status": "review", "claimed_by": "GRPH-A1",
               "review_claimed_by": "", "prd_id": "GRPH-P1"}]
    result, _ = _line_run(git_repo, tmp_path, scripts, state, review=review,
                          minted_args=mints, staff_every_row=True)
    assert result.spawned >= 1, result.as_json()
    assert mints and all(m.get("review_only") for m in mints), mints
    assert all(m.get("line") == "GRPH-L1" and not m.get("scope") for m in mints), mints
    assert result.review == ["GRPH-5"]
    assert result.as_json()["review_outside_prd"] == ["GRPH-6"]


def test_a_line_drain_delegates_line_items_only_and_never_sends_line_to_delegate(
        git_repo, tmp_path, scripts, state):
    """`collision_clusters` has no `line_id` and `delegate` has no `line`: the server drops
    both. So the clusters are filtered here to the line's members, and the delegated seat is
    bound `bound_only` so it cannot claim or review past its line item.

    Sabotage: return `got` unfiltered from `_clusters` and the PRD item GRPH-2 is delegated."""
    delegations: list[dict] = []
    backlog = [{"id": "GRPH-1", "status": "next", "line_id": "GRPH-L1",
                "description": "## Acceptance\n- ok\n", "touchpoints": ["a.py"]},
               {"id": "GRPH-2", "status": "next", "prd_id": "GRPH-P1",
                "description": "## Acceptance\n- ok\n", "touchpoints": ["b.py"]}]
    result, calls = _line_run(git_repo, tmp_path, scripts, state, clusters=2,
                              cluster_items=[["GRPH-2"], ["GRPH-1"]], backlog=backlog,
                              delegations=delegations)
    assert [d["id"] for d in delegations] == ["GRPH-1"], delegations
    assert all("line" not in d and "line_id" not in d for d in delegations), delegations
    assert all(d.get("bound_only") is True and not d.get("scope") for d in delegations)
    assert result.as_json()["unready"] == []


def test_clusters_filter_keeps_only_line_members():
    class _P:
        def call(self, tool, **kw):
            assert "line_id" not in kw and "line" not in kw, kw
            if tool == "collision_clusters":
                return {"clusters": [{"items": ["A", "B"]}, {"items": ["C"]}], "total": 2}
            if kw.get("status") == "next":
                return {"results": [{"id": "A", "line_id": "L"}, {"id": "C", "prd_id": "P"}]}
            return {"results": []}

    got = until._clusters(_P(), None, "L")
    assert got["clusters"] == [{"items": ["A"]}] and got["total"] == 1


def test_an_item_in_the_claim_pool_is_not_unready():
    """WHICH items are unready is the server's verdict: one `collision_clusters` still offers
    is claimable whatever this client's parse of its description says."""
    class _P:
        def call(self, tool, **kw):
            if tool == "collision_clusters":
                return {"clusters": [{"items": ["A"]}]}
            if kw.get("status") == "next":
                return {"results": [
                    {"id": "A", "line_id": "L", "description": "Tests:\n- x", "touchpoints": []},
                    {"id": "B", "line_id": "L", "description": "", "touchpoints": ["b"]},
                    {"id": "C", "line_id": "L", "description": "", "touchpoints": [],
                     "blocker": "waits on a person"}]}
            return {"results": []}

    assert until._unready(_P(), "L") == [{"id": "B", "missing": ["acceptance"]}]


@pytest.mark.parametrize("text", [
    "x\n## Acceptance\n- a", "### Acceptance criteria\n\n1. a", "Acceptance:\n- a",
    "**Acceptance:**\n- a", "__Acceptance__\n* a", "## Tests\n- a"])
def test_acceptance_shapes_the_server_reads_are_read(text):
    assert until._ACCEPT_RE.search(text)


class _DryP:
    def __init__(self, ml=LINE):
        self.ml = ml

    def call(self, tool, **kw):
        if tool == "get_context":
            return {"maintenance_line": self.ml} if self.ml else {}
        if tool == "collision_clusters":
            return {"clusters": [{"items": ["P-1"]}, {"items": ["L-1"]}], "holds": []}
        if tool == "search_items":
            if kw.get("status") == "next":
                return {"results": [{"id": "L-1", "line_id": "GRPH-L1"},
                                    {"id": "P-1", "prd_id": "P"}]}
            return {"results": []}
        return {}


def test_dry_run_plan_with_line_lists_only_the_line_item():
    """GRPH-1710. Sabotage: call plan() without `line` and `would_delegate` gains P-1."""
    got = until.plan(_DryP(), None, 5, line="")
    assert got["would_delegate"] == ["L-1"]
    assert got["line"] == "GRPH-L1" and got["seats_scoped"] is True


def test_dry_run_plan_refuses_a_prd_id_and_a_missing_line():
    with pytest.raises(ConfigError, match="use --prd"):
        until.plan(_DryP(), None, 5, line="GRPH-P86")
    with pytest.raises(ConfigError, match="create_line"):
        until.plan(_DryP(ml=None), None, 5, line="")
