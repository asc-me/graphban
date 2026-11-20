"""GRPH-1743: a red-CI bounce note names the failing test when gh refuses ANSI logs.

Current `gh api` will not print a job log that contains terminal escapes unless
`--allow-escape-sequences` is passed. Both job-log reads (`transport_flake_jobs`
and `failed_test_summary`, which `_ci_attested` calls before it bounces) pass
that flag, then `_ANSI` strips SGR codes before the FAILED-line parse.

Sabotage: drop `--allow-escape-sequences` from the job-log read in
`failed_test_summary` and
`test_bounce_note_names_the_failed_test_when_gh_requires_the_escape_flag` fails:
the note says the log could not be read and does not name tests/x.py::test_y.
Dropping it from `transport_flake_jobs` fails the same test, because that read
is the other call on this path.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until

HEAD = "abcdef1234567890"
REFUSAL = (
    "the response contains terminal escape sequences; "
    "pass --allow-escape-sequences to output it anyway"
)
LOG = (
    "2026-11-19T10:00:00.0000000Z \x1b[31mFAILED\x1b[0m tests/x.py::test_y"
    " - \x1b[1;31mNameError\x1b[0m\n"
    "\x1b[32m===== 1 failed, 3 passed in 1.2s =====\x1b[0m\n"
)
ROW = {"id": "GRPH-1742", "branch": "gb/x", "bounce_reason": ""}


def _cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _forge(monkeypatch):
    rollup = {"statusCheckRollup": [
        {"name": "fleet", "conclusion": "FAILURE", "detailsUrl": "https://g/runs/9/job/77"},
    ]}
    calls = []

    def fake(repo, *args, **kw):
        calls.append(args)
        if args and args[0] == "pr":
            return _cp(json.dumps(rollup))
        if args and args[0] == "api" and "--allow-escape-sequences" not in args:
            return _cp(rc=1, err=REFUSAL)
        return _cp(LOG)

    monkeypatch.setattr(propose, "_gh", fake)
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "fleet"))
    monkeypatch.setattr(until.observe, "emit", lambda *a, **k: None)
    return calls


class _Planner:
    def __init__(self):
        self.calls = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        return {}


def test_bounce_note_names_the_failed_test_when_gh_requires_the_escape_flag(monkeypatch):
    calls = _forge(monkeypatch)
    planner = _Planner()
    ready, waiting, blocked = until._ci_attested(
        [ROW], Path("."), set(), planner=planner, agent_id="GRPH-A1")
    assert (ready, waiting, blocked) == ([], [], [ROW])
    tool, args = next((tool, args) for tool, args in planner.calls if tool == "bounce")
    assert tool == "bounce"
    note = args["reason"]
    assert "tests/x.py::test_y" in note
    assert "NameError" in note
    assert "\x1b" not in note
    assert "terminal escape sequences" not in note
    assert until.REVIEW_CI_RED in note
    log_calls = [a for a in calls if a and a[0] == "api" and "/logs" in str(a[1])]
    # The caller reads the log twice: once to decide it is not a transport flake,
    # once to name the failing tests on the bounce.
    assert len(log_calls) == 2
    assert all("--allow-escape-sequences" in a for a in log_calls)
    assert all(
        a[:2] == ("api", "repos/{owner}/{repo}/actions/jobs/77/logs") for a in log_calls
    )
