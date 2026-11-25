"""GRPH-1919, through the loop: what `_settle_exited` says is what the drain does.

The unit tests in `test_until.py` call `_settle_exited` directly. These drive `until.run`
(and so `_loop`) over a mock server, with the real `_settle_exited`, so the two places the
loop consumes it are guarded:

- its wait lines are folded into `waits`, so a touchpoint-gated exited child's item makes
  the exit `idle-with-waits` and is named, not `idle`;
- its "CI still running" verdict keeps the loop polling instead of exiting.

The exited child is seeded into the loop's own `exited` map on the first settle call, as
`_track_exits` does when a real child is reaped — everything after that is the loop's.

Sabotage: `if False and wait_line not in waits:` on the `exit_lines` fold in `_loop` — the
gated test exits `idle`. `if ci_pending:` in place of `if ci_pending or exit_ci_pending:` —
the pending test exits on its first empty poll.
"""
from __future__ import annotations

from pathlib import Path

import gbfleet.until as until_mod
from gbfleet.until import run

from tests.test_merge_after_sign_off import _server
from tests.test_supervisor import _factory
from tests.test_until import _Exited

GATE = "changes files outside every declared touchpoint: a.py"


def _seed(monkeypatch, item: str, branch: str, gate: str | None = None) -> dict:
    real = until_mod._settle_exited
    seen = {"settles": 0}

    def settle(planner, exited, wave, repo, agent_id):
        if seen["settles"] == 0:
            until_mod._track_exits(exited, [_Exited(item, branch)])
            if gate:
                wave.unproposed[branch] = gate
        seen["settles"] += 1
        return real(planner, exited, wave, repo, agent_id)

    monkeypatch.setattr(until_mod, "_settle_exited", settle)
    return seen


def _drain(git_repo, scripts, state, workspace, planner, supervisor, sleep):
    return run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key="k", server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=1,
    )


def test_a_gated_exited_childs_item_makes_the_drain_idle_with_waits_and_is_named(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1910's production symptom: its child exited, its branch sat behind the
    touchpoint gate, and the drain said `idle`. It must say `idle-with-waits` and name it."""
    items = {"GRPH-1910": {"id": "GRPH-1910", "status": "in_progress",
                           "branch": "gb/line-407", "evidence": []}}
    monkeypatch.setattr(until_mod, "ci_verdict",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("gh asked")))
    seen = _seed(monkeypatch, "GRPH-1910", "gb/line-407", gate=GATE)
    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items)

    result = _drain(git_repo, scripts, state, workspace, planner, supervisor,
                    sleep=lambda _p: None)

    assert seen["settles"] >= 1, "the loop never settled an exited child"
    assert result.reason == "idle-with-waits", (result.reason, result.waits)
    assert result.exit == 0
    assert (f"GRPH-1910: in_progress after its child qwen pid 2480 (GRPH-A47583) exited; "
            f"gb/line-407 not proposed — {GATE}") in result.waits


def test_an_exited_childs_pr_with_ci_running_keeps_the_drain_polling(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """`empty_ticks=1` would exit on the first empty poll. An exited child's PR whose CI is
    still running is not empty: the loop sleeps and reads again until CI stops running."""
    url = "https://github.com/o/r/pull/2244"
    items = {"GRPH-1901": {"id": "GRPH-1901", "status": "in_progress",
                           "branch": "gb/line-407",
                           "evidence": [{"kind": "url", "url": url, "detail": "draft PR"}]}}
    verdicts = {"n": 0}

    def ci_verdict(*_a):
        verdicts["n"] += 1
        return ("pending", "", "") if verdicts["n"] <= 3 else ("none", "", "")

    monkeypatch.setattr(until_mod, "ci_verdict", ci_verdict)
    seen = _seed(monkeypatch, "GRPH-1901", "gb/line-407")
    slept = {"n": 0, "pending_then": []}

    def sleep(_poll):
        slept["n"] += 1
        slept["pending_then"].append(verdicts["n"])
        if slept["n"] > 50:
            raise AssertionError("the drain never stopped polling")

    workspace = tmp_path / "ws"
    planner, supervisor = _server(workspace, items=items)

    result = _drain(git_repo, scripts, state, workspace, planner, supervisor, sleep=sleep)

    assert seen["settles"] >= 4, seen
    # Every poll while CI was pending was slept on, not exited.
    assert verdicts["n"] >= 4, verdicts
    assert [v for v in slept["pending_then"] if v <= 3] == [1, 2, 3], slept
    # Once CI stopped running the item is still a named wait, so not `idle`.
    assert result.reason == "idle-with-waits", (result.reason, result.waits)
    assert any(w.startswith("GRPH-1901: in_progress after its child") and url in w
               for w in result.waits), result.waits
