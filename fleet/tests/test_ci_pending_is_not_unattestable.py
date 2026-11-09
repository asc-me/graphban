"""GRPH-1417: a CI run still in progress for the head is pending, not unattestable."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import until as until_mod
from gbfleet.supervisor import Limits
from gbfleet.until import ci_run_in_progress, run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _CI_STUCK, _clients

ROW = {"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": "",
       "branch": "gb/x", "evidence": [_CI_STUCK]}


def _go(git_repo, tmp_path, scripts, state, sleep):
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(workspace, review=[dict(ROW)], minted_roles=roles)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"), planner, supervisor,
        api_key=KEY, server="http://gb.invalid", adapter="fake", state=state,
        workspace=workspace, poll=0, sleep=sleep, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    return result, roles


def test_in_progress_run_keeps_polling_and_records_a_wait(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    calls = {"n": 0}

    def fake(repo, row):
        return "run 77 CI in_progress" if calls["n"] < 10 else ""

    def sleep(_):
        calls["n"] += 1

    monkeypatch.setattr(until_mod, "ci_run_in_progress", fake)
    result, roles = _go(git_repo, tmp_path, scripts, state, sleep)
    assert roles == []
    assert calls["n"] >= 10  # it did not exit while the run was pending
    assert result.reason == "review-ci-unattestable"  # only once the run finished silent
    assert any("review_waits_on_ci" in w and "run 77" in w for w in result.wave.review_waits)


def test_no_run_for_the_head_still_exits_unattestable(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    monkeypatch.setattr(until_mod, "ci_run_in_progress", lambda repo, row: "")
    result, roles = _go(git_repo, tmp_path, scripts, state, lambda _: None)
    assert roles == []
    assert result.reason == "review-ci-unattestable"


def _gh(monkeypatch, runs):
    def fake(*a, **k):
        return subprocess.CompletedProcess(a, 0, json.dumps(runs), "")
    monkeypatch.setattr(subprocess, "run", fake)


def test_gh_lookup_matches_head_and_status(tmp_path: Path, monkeypatch):
    _gh(monkeypatch, [{"databaseId": 5, "status": "in_progress", "name": "CI",
                       "headSha": "5f4e0ecc" + "0" * 32}])
    assert "5" in ci_run_in_progress(tmp_path, ROW)
    _gh(monkeypatch, [{"databaseId": 5, "status": "completed", "name": "CI",
                       "headSha": "5f4e0ecc" + "0" * 32}])
    assert ci_run_in_progress(tmp_path, ROW) == ""
    _gh(monkeypatch, [{"databaseId": 5, "status": "in_progress", "name": "CI",
                       "headSha": "deadbeef" + "0" * 32}])
    assert ci_run_in_progress(tmp_path, ROW) == ""
