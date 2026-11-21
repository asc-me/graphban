"""A signed-off merge that can still clear is not an idle drain (GRPH-1577).

CI that has not attested ``suite_green`` stays in the drain and is retried once it
does. A stop while that item is unmerged reports ``merge-pending``, not ``idle``.

Sabotage: delete the ``merge-pending`` continue before ``empty += 1`` in
``until._loop``. The retry test exits ``idle`` with no ``pr merge``.
"""
from __future__ import annotations

import pytest

from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet.until import run

from tests.test_merge_after_sign_off import _done_item, _only_gh, _run, _server
from tests.test_propose import REVIEWED, _Forge, _att, _opened
from tests.test_supervisor import _factory


@pytest.fixture()
def forge(monkeypatch):
    """`gh` only. The loop under test also runs real git through `subprocess`."""
    def _wire(fake):
        monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
        monkeypatch.setattr(propose_mod.subprocess, "run", _only_gh(fake))
        return fake
    return _wire


def _awaiting_ci(item_id: str = "SA-1573") -> dict:
    """Signed off, PR open, CI not attested yet. The miss the drain used to treat as done."""
    item = _done_item(item_id, status="done")
    item["evidence"] = [e for e in item["evidence"] if e.get("adapter") != "github-actions"]
    item["evidence"].insert(0, _opened(9, item["branch"]))
    item["pr"] = "https://github.com/o/r/pull/9"
    return item


def test_ci_that_attests_on_the_next_cycle_is_merged_and_idle_is_not_early(
    git_repo, tmp_path, scripts, state, forge, monkeypatch,
):
    """empty_ticks=1 would exit idle on the first empty poll. The miss is not empty:
    the next poll, once suite_green is on the item, merges.

    Sabotage: skip the pending continue. This returns idle and never calls ``pr merge``."""
    monkeypatch.setattr(sup, "MERGE_RECHECK_S", 0)
    gh = forge(_Forge(draft=True))
    items = {"SA-1573": _awaiting_ci()}
    injected = {"n": 0}

    def sleep(_poll):
        viewed = any(c[1:3] == ["pr", "view"] for c in gh.calls)
        merged = any(c[1:3] == ["pr", "merge"] for c in gh.calls)
        if viewed and not merged and injected["n"] == 0:
            items["SA-1573"]["evidence"].append(
                _att("github-actions", REVIEWED, ("suite_green", True)))
            injected["n"] += 1

    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key="k", server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=1,
        merge=True,
    )

    assert injected["n"] == 1, "CI was never given a later poll to arrive on"
    assert any(c[1:3] == ["pr", "merge"] for c in gh.calls), result.reason
    assert result.wave.merged["SA-1573"].ok
    assert result.reason == "idle", result.detail


def test_a_stop_while_ci_has_not_attested_reports_merge_pending(
    git_repo, tmp_path, scripts, state, forge, monkeypatch,
):
    """The process has to stop before CI attests. The reason names the item.
    ``idle`` would read as a drained PRD."""
    monkeypatch.setattr(sup, "MERGE_RECHECK_S", 0)
    gh = forge(_Forge(draft=True))
    items = {"SA-1573": _awaiting_ci()}
    clock = {"t": 0.0}
    jumped = {"n": 0}

    def now():
        return clock["t"]

    def sleep(_poll):
        if any(c[1:3] == ["pr", "view"] for c in gh.calls) and jumped["n"] == 0:
            clock["t"] += 1000.0
            jumped["n"] += 1

    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key="k", server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=1,
        merge=True, stall_after=30, clock=now,
    )

    assert jumped["n"] == 1
    assert result.reason == "merge-pending", result.detail
    assert result.exit == 0 and result.ok is False
    named = result.detail + " " + " ".join(result.waits)
    assert "SA-1573" in named and "suite_green" in named
    assert not any(c[1:3] == ["pr", "merge"] for c in gh.calls)
    assert result.as_json()["reason"] == "merge-pending"


def test_a_finished_merge_still_exits_idle(git_repo, tmp_path, scripts, state, forge):
    """The wait is only while the merge is outstanding. A merge that lands is idle."""
    gh = forge(_Forge(draft=True))
    items = {"SA-417": _done_item("SA-417", status="done")}
    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items)

    result = _run(git_repo, scripts, state, workspace, planner, supervisor, merge=True)

    assert result.reason == "idle"
    assert result.wave.merged["SA-417"].ok
    assert "pr merge" in [" ".join(c[1:3]) for c in gh.calls]
    assert result.wave.merge_pending == []
