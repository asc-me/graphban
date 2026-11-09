"""The drain holds a reviewer spawn until CI has finished at the head (GRPH-1392).

Sabotage: make `_ci_attested` return `(rows, [], [])` and the pending tests fail; drop the
`if ci_waiting:` branch in `_loop` and the loop-level pending test ends `review-unsigned`
before CI turns green.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until
from gbfleet.client import NotPermitted
from gbfleet.supervisor import Limits
from gbfleet.until import _ci_attested, run
from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

ROW = {"id": "GRPH-1", "branch": "gb/x"}
HEAD = "abcdef1234567890"


def _verdict(monkeypatch, state, head=HEAD, job=""):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: (state, head, job))


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: seen.append((ev, kw)))
    return seen


class _Planner:
    def __init__(self, refuse: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.refuse = refuse

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        if self.refuse:
            raise NotPermitted("update_item refused")
        return {}


# --- the split --------------------------------------------------------------------------


def test_pending_ci_holds_the_spawn_and_logs_the_commit(monkeypatch):
    _verdict(monkeypatch, "pending")
    seen = _events(monkeypatch)
    logged: set = set()
    assert _ci_attested([ROW], Path("."), logged) == ([], [ROW], [])
    assert _ci_attested([ROW], Path("."), logged) == ([], [ROW], [])
    assert [e for e, _ in seen] == ["review_waits_on_ci"]  # once per head
    assert seen[0][1]["commit"] == HEAD


def test_green_ci_spawns(monkeypatch):
    _verdict(monkeypatch, "green")
    seen = _events(monkeypatch)
    assert _ci_attested([ROW], Path("."), set()) == ([ROW], [], [])
    assert seen == []


def test_red_ci_blocks_the_item_on_the_failing_job(monkeypatch):
    _verdict(monkeypatch, "red", job="fleet-tests")
    seen = _events(monkeypatch)
    planner = _Planner()
    got = _ci_attested([ROW], Path("."), set(), planner=planner, agent_id="GRPH-P1")
    assert got == ([], [], [ROW])  # no reviewer for it
    assert [e for e, _ in seen] == ["review_ci_red", "review_ci_red_blocked"]
    assert seen[0][1]["job"] == "fleet-tests"
    (tool, args), = planner.calls
    assert tool == "update_item" and args["id"] == "GRPH-1" and args["status"] == "blocked"
    assert "fleet-tests" in args["blocker"] and HEAD[:12] in args["blocker"]
    assert args["agent_id"] == "GRPH-P1"


def test_a_refused_block_falls_back_to_a_reviewer_who_can_bounce(monkeypatch):
    _verdict(monkeypatch, "red", job="fleet-tests")
    seen = _events(monkeypatch)
    got = _ci_attested([ROW], Path("."), set(), planner=_Planner(refuse=True))
    assert got == ([ROW], [], [])
    assert "review_ci_red_unrecorded" in [e for e, _ in seen]


def test_unknown_ci_does_not_wedge_the_queue(monkeypatch):
    _verdict(monkeypatch, "unknown", head="")
    assert _ci_attested([ROW], Path("."), set()) == ([ROW], [], [])


# --- reading the rollup -------------------------------------------------------------------


def _gh_answers(monkeypatch, payload: dict):
    monkeypatch.setattr(propose, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose, "_gh", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=json.dumps(payload), stderr=""))


def test_a_pr_with_no_checks_is_unknown_not_pending(monkeypatch):
    _gh_answers(monkeypatch, {"headRefOid": HEAD, "statusCheckRollup": []})
    assert propose.ci_rollup(Path("."), "gb/x")[0] == "unknown"
    assert propose.ci_green(Path("."), "gb/x", HEAD) is False  # still fails closed


def test_the_rollup_reads_pending_green_and_red(monkeypatch):
    _gh_answers(monkeypatch, {"headRefOid": HEAD, "statusCheckRollup": [
        {"name": "a", "conclusion": "SUCCESS"}, {"name": "b", "status": "IN_PROGRESS"}]})
    assert propose.ci_rollup(Path("."), "gb/x") == ("pending", HEAD, "")
    _gh_answers(monkeypatch, {"headRefOid": HEAD, "statusCheckRollup": [
        {"name": "a", "conclusion": "SUCCESS"}, {"context": "lint", "state": "SKIPPED"}]})
    assert propose.ci_rollup(Path("."), "gb/x") == ("green", HEAD, "")
    assert propose.ci_green(Path("."), "gb/x", HEAD) is True
    assert propose.ci_green(Path("."), "gb/x", "other") is False
    _gh_answers(monkeypatch, {"headRefOid": HEAD, "statusCheckRollup": [
        {"name": "a", "conclusion": "SUCCESS"}, {"name": "fleet-tests", "conclusion": "FAILURE"}]})
    assert propose.ci_rollup(Path("."), "gb/x") == ("red", HEAD, "fleet-tests")


# --- the loop -----------------------------------------------------------------------------

REVIEW = [{"id": "GRPH-9", "status": "review", "branch": "gb/p-9",
           "claimed_by": "GRPH-A1", "review_claimed_by": ""}]


def test_the_loop_polls_through_pending_ci_then_spawns_on_green(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """The motivating case: the only item left, CI still running after a bounce fix, nothing
    live. Before the fix the drain ended `review-unsigned` without waiting."""
    order: list[str] = []
    answers = iter(["pending"] * 4)

    def verdict(*_a):
        state_ = next(answers, "green")
        order.append(f"ci:{state_}")
        return state_, HEAD, ""

    monkeypatch.setattr(until, "ci_verdict", verdict)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, review=REVIEW,
                                   on_mint=lambda role: order.append("mint"))
    sleeps: list = []
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleeps.append, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    first_mint = order.index("mint")
    assert order[:first_mint] == ["ci:pending"] * 4 + ["ci:green"], order
    assert len(sleeps) >= 4  # it waited, rather than exiting
    # The spawned children never sign, so the run still ends unsigned — after spawning.
    assert result.reason == "review-unsigned"
    assert result.spawned >= 1


def test_the_loop_blocks_a_red_row_and_spawns_no_reviewer(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "fleet-tests"))
    workspace = tmp_path / "ws"
    roles: list[str] = []
    calls: list[str] = []
    planner, supervisor = _clients(workspace, review=REVIEW, minted_roles=roles, calls=calls)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles == []
    assert "update_item" in calls
    assert result.spawned == 0
    assert result.reason != "review-unsigned"
