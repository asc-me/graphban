"""GRPH-1757: a third-party preview status does not bounce a green Actions rollup.

Sabotage: make `propose.is_actions_job` return True for every dict and
`test_external_status_red_does_not_bounce` fails — the Unkey status is then an
Actions failure, `ci_rollup` answers red, and the row is bounced.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until
from gbfleet.until import _ci_attested

ROW = {"id": "GRPH-1", "branch": "gb/x"}
HEAD = "abcdef1234567890"
UNKEY = "Unkey / graphban-console/console - preview"

ACTIONS_OK = {
    "__typename": "CheckRun",
    "name": "CI",
    "workflowName": "CI",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2",
}
UNKEY_STATUS = {
    "__typename": "StatusContext",
    "context": UNKEY,
    "state": "FAILURE",
    "targetUrl": "https://app.unkey.com/deployments/x",
}
UNKEY_CHECK_RUN = {
    "__typename": "CheckRun",
    "name": UNKEY,
    "workflowName": "",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "detailsUrl": "https://app.unkey.com/deployments/x",
}
ACTIONS_FAIL = {
    "__typename": "CheckRun",
    "name": "fleet-tests",
    "workflowName": "CI",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9",
}


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: seen.append((ev, kw)))
    return seen


class _Planner:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        return {}


def _gh(monkeypatch, rollup, *, rules=None, rules_rc=0):
    """A fake `gh`: `pr view` returns the rollup; the rules endpoint returns `rules`.

    `rules is None` with `rules_rc == 0` is a successful read of no required checks.
    A non-zero `rules_rc` is an unreadable required set, which is not that.
    """
    def fake(repo, *args, **kw):
        if args[:2] == ("pr", "view"):
            body = json.dumps({
                "headRefOid": HEAD,
                "baseRefName": "main",
                "statusCheckRollup": rollup,
            })
            return subprocess.CompletedProcess(args, 0, body, "")
        joined = " ".join(str(a) for a in args)
        if args and args[0] == "api" and "rules/branches" in joined:
            if rules_rc != 0:
                return subprocess.CompletedProcess(args, rules_rc, "", "rules unread")
            body = json.dumps([] if rules is None else rules)
            return subprocess.CompletedProcess(args, 0, body, "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected " + joined)

    monkeypatch.setattr(propose, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose, "_gh", fake)
    # The suite pins ci_verdict to unknown so no test shells out. This one is the reading.
    monkeypatch.setattr(until, "ci_verdict",
                        lambda repo, branch: propose.ci_rollup(repo, branch))


def _rules(*contexts: str) -> list[dict]:
    return [{
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
    }]


def _attested(monkeypatch, rollup, **kw):
    _gh(monkeypatch, rollup, **kw)
    seen = _events(monkeypatch)
    planner = _Planner()
    got = _ci_attested([ROW], Path("."), set(), planner=planner, agent_id="GRPH-P1")
    return got, seen, planner


def test_external_status_red_does_not_bounce(monkeypatch):
    for shape in (UNKEY_STATUS, UNKEY_CHECK_RUN):
        got, seen, planner = _attested(monkeypatch, [ACTIONS_OK, shape])
        assert got == ([ROW], [], []), shape
        assert not any(tool == "bounce" for tool, _ in planner.calls)
        logged = [(ev, kw) for ev, kw in seen if ev == "ci_red_external"]
        assert logged, seen
        assert UNKEY in logged[0][1]["job"]
        assert UNKEY in logged[0][1]["detail"]
        assert "review_ci_red_bounced" not in [ev for ev, _ in seen]
        assert propose.ci_green(Path("."), "gb/x", HEAD) is False


def test_a_failing_actions_job_still_bounces(monkeypatch):
    got, seen, planner = _attested(
        monkeypatch, [ACTIONS_FAIL, {**UNKEY_STATUS, "state": "SUCCESS"}])
    assert got == ([], [], [ROW])
    assert "review_ci_red_bounced" in [ev for ev, _ in seen]
    assert "ci_red_external" not in [ev for ev, _ in seen]
    (tool, args), = [c for c in planner.calls if c[0] == "bounce"]
    assert tool == "bounce"
    assert "fleet-tests" in args["reason"]
    assert args["reason"].startswith(until.CI_RED_BOUNCE)


def test_a_required_external_check_still_bounces(monkeypatch):
    got, seen, planner = _attested(
        monkeypatch, [ACTIONS_OK, UNKEY_STATUS], rules=_rules(UNKEY))
    assert got == ([], [], [ROW])
    assert "ci_red_external" not in [ev for ev, _ in seen]
    (tool, args), = [c for c in planner.calls if c[0] == "bounce"]
    assert UNKEY in args["reason"]


def test_unreadable_required_checks_are_named_and_do_not_bounce(monkeypatch):
    got, seen, planner = _attested(
        monkeypatch, [ACTIONS_OK, UNKEY_STATUS], rules_rc=1)
    assert got == ([ROW], [], [])
    assert not any(tool == "bounce" for tool, _ in planner.calls)
    detail = next(kw["detail"] for ev, kw in seen if ev == "ci_red_external")
    assert "required checks unread" in detail
    assert UNKEY in detail


def test_actions_still_running_waits_on_the_external_status(monkeypatch):
    running = {**ACTIONS_OK, "status": "IN_PROGRESS", "conclusion": ""}
    got, seen, planner = _attested(monkeypatch, [running, UNKEY_STATUS])
    assert got == ([], [ROW], [])
    assert not any(tool == "bounce" for tool, _ in planner.calls)
    assert "review_waits_on_ci" in [ev for ev, _ in seen]
    assert "ci_red_external" not in [ev for ev, _ in seen]


def test_a_green_head_lifts_the_external_hold(monkeypatch):
    seen = _events(monkeypatch)
    planner = _Planner()
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("green", HEAD, ""))
    row = {**ROW, "blocker": until.external_hold_text(UNKEY)}
    got = _ci_attested([row], Path("."), set(), planner=planner, agent_id="GRPH-P1")
    assert got == ([row], [], [])
    assert not any(tool == "bounce" for tool, _ in planner.calls)
    (tool, args), = planner.calls
    assert tool == "update_item" and args["blocker"] == ""
    assert "ci_red_external_lifted" in [ev for ev, _ in seen]


def test_persistent_external_red_is_a_named_hold_not_a_bounce(monkeypatch):
    _gh(monkeypatch, [ACTIONS_OK, UNKEY_STATUS])
    seen = _events(monkeypatch)
    planner = _Planner()
    logged: set = set()
    for _ in range(until.EXTERNAL_RED_HOLD_AFTER - 1):
        got = _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="GRPH-P1")
        assert got == ([ROW], [], [])
        assert not any(tool == "bounce" for tool, _ in planner.calls)
    got = _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="GRPH-P1")
    assert got == ([], [ROW], [])
    assert not any(tool == "bounce" for tool, _ in planner.calls)
    holds = [args for tool, args in planner.calls if tool == "update_item"]
    assert holds
    blocker = holds[-1]["blocker"]
    assert UNKEY in blocker
    assert until.EXTERNAL_HOLD_MARK in blocker
    assert not until.is_builder_ci_blocker(blocker)
    assert "ci_red_external_hold" in [ev for ev, _ in seen]
    # The same head does not bounce, and does not write the hold again.
    again = _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="GRPH-P1")
    assert again == ([], [ROW], [])
    assert [tool for tool, _ in planner.calls].count("update_item") == 1
    assert not any(tool == "bounce" for tool, _ in planner.calls)
