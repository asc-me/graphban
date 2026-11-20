"""GRPH-1758: a CI-red note names the failing tests when the job log is coloured.

`gh api` refuses an Actions log that contains ANSI unless `--allow-escape-sequences` is
passed. The fake `gh` exits nonzero with that refusal when the flag is missing, so a log
read that omits it cannot produce the node ids.

Sabotage: drop `--allow-escape-sequences` from either job-log read in `until.py` and
`test_review_ci_red_note_names_failed_tests_from_a_coloured_log` goes red.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until

HEAD = "abcdef1234567890"
ESCAPE_REFUSAL = (
    "the response contains terminal escape sequences; "
    "pass --allow-escape-sequences to output it anyway"
)
LOG = (
    "2026-11-19T12:00:00.0000000Z \x1b[31mFAILED\x1b[0m tests/test_fleet.py::test_one"
    " - AssertionError: a\n"
    "2026-11-19T12:00:01.0000000Z \x1b[31mFAILED\x1b[0m tests/test_ci.py::test_two"
    " - AssertionError: b\n"
    "2026-11-19T12:00:02.0000000Z \x1b[32m===== 2 failed, 11 passed in 4.0s =====\x1b[0m\n"
)
ROW = {"id": "GRPH-1", "branch": "gb/x", "bounce_reason": ""}


def _cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _forge(monkeypatch):
    rollup = {"statusCheckRollup": [
        {"name": "Fleet supervisor tests", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/asc-me/graphban/actions/runs/9/job/77"},
    ]}
    calls = []

    def fake(repo, *args, **kw):
        calls.append(args)
        if args[0] == "pr":
            return _cp(json.dumps(rollup))
        if args[0] == "api" and "--allow-escape-sequences" not in args:
            return _cp(rc=1, err=ESCAPE_REFUSAL)
        return _cp(LOG)

    monkeypatch.setattr(propose, "_gh", fake)
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "Fleet supervisor tests"))
    monkeypatch.setattr(until.observe, "emit", lambda *a, **k: None)
    return calls


class _Planner:
    def __init__(self):
        self.calls = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        return {}


def test_review_ci_red_note_names_failed_tests_from_a_coloured_log(monkeypatch):
    calls = _forge(monkeypatch)
    planner = _Planner()
    ready, waiting, blocked = until._ci_attested(
        [ROW], Path("."), set(), planner=planner, agent_id="P")
    assert (ready, waiting, blocked) == ([], [], [ROW])
    log_calls = [a for a in calls if a and a[0] == "api" and "/logs" in a[1]]
    # transport_flake_jobs and failed_test_summary each read the job log.
    assert len(log_calls) == 2
    assert all("--allow-escape-sequences" in a for a in log_calls)
    reason = next(args["reason"] for tool, args in planner.calls if tool == "bounce")
    assert until.REVIEW_CI_RED in reason
    assert "tests/test_fleet.py::test_one" in reason
    assert "tests/test_ci.py::test_two" in reason
    assert "2 failed, 11 passed" in reason
    assert "\x1b" not in reason
    assert "terminal escape sequences" not in reason
