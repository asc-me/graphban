"""GRPH-1743: a red-CI bounce note names the failing test when gh refuses ANSI logs.

Current `gh api` will not print a job log that contains terminal escapes unless
`--allow-escape-sequences` is passed. The reader must pass that flag, then strip
SGR codes (`\\x1b\\[[0-9;]*m`, covered by `_ANSI`) before the FAILED-line parse.

Sabotage: drop `--allow-escape-sequences` from `_fetch_job_log` and
`test_bounce_note_names_the_failed_test_when_gh_requires_the_escape_flag` fails.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until
from gbfleet.until import _return_or_block_ci_red

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
        if "--allow-escape-sequences" not in args:
            return _cp(rc=1, err=REFUSAL)
        return _cp(LOG)

    monkeypatch.setattr(propose, "_gh", fake)
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
    row = {"id": "GRPH-1742", "bounce_reason": ""}
    assert _return_or_block_ci_red(
        planner, row, HEAD, "fleet", "GRPH-A1", repo=Path("."), branch="gb/x")
    tool, args = planner.calls[0]
    assert tool == "bounce"
    note = args["reason"]
    assert "tests/x.py::test_y" in note
    assert "NameError" in note
    assert "\x1b" not in note
    assert until.REVIEW_CI_RED in note
    assert any(
        a[:2] == ("api", "repos/{owner}/{repo}/actions/jobs/77/logs")
        and "--allow-escape-sequences" in a
        for a in calls
    )
