"""GRPH-1438: a cap exit must not abandon a child that is still running.

Design: `until` waits on a cap (it stops spawning, keeps reaping) and exits after the last
live child is reaped, which is where its branch is published. It does not hand the child to
a successor, so the cap summary names the children it waited for instead.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import until
from gbfleet.supervisor import Limits
from gbfleet.until import CapError, run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients


class _LiveChild:
    """Stands in for a Child: `running` until the fake tick below finishes it."""

    def __init__(self):
        self.running = True
        self.local_commit = True
        self.on_remote = False


def _run_with_live_child(monkeypatch, git_repo, tmp_path, scripts, state, ticks_to_finish=3):
    child = _LiveChild()
    ticks = {"n": 0}

    def fake_loop(wave, children, *a, **kw):
        children.append(child)  # spawned before the cap was hit
        raise CapError("cap", "max_children 8 reached: 8 item(s) across 8 process(es)")

    def fake_tick(wave, children, *a, **kw):
        ticks["n"] += 1
        if ticks["n"] >= ticks_to_finish:
            child.running = False

    def fake_reap(wave, finished, **kw):
        for c in finished:
            c.on_remote = c.local_commit  # the normal reap path pushes unpushed commits

    monkeypatch.setattr(until, "_loop", fake_loop)
    monkeypatch.setattr(until, "watch_tick", fake_tick)
    monkeypatch.setattr(until, "_report_exits", lambda *a, **k: None)
    monkeypatch.setattr(until, "_reap_all", fake_reap)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=8),
    )
    return result, child, ticks


def test_cap_exit_waits_for_live_child_and_its_commit_reaches_the_remote(
    monkeypatch, git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    result, child, ticks = _run_with_live_child(monkeypatch, git_repo, tmp_path, scripts, state)
    assert result.reason == "cap" and result.exit == 1
    assert not child.running, "until exited with its child still live"
    assert child.on_remote, "the child's commit was never published"
    assert ticks["n"] == 3


def test_cap_summary_names_the_children_it_waited_for(
    monkeypatch, git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    result, _child, _ticks = _run_with_live_child(monkeypatch, git_repo, tmp_path, scripts, state)
    assert "max_children 8" in result.detail
    assert "waited for 1 live child" in result.detail
