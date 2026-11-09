"""GRPH-1458: a red-CI blocker names the failing tests, read from the job log on the planner.

Sabotage: drop the `propose._gh(repo, "api", ...)` log fetch in `failed_test_summary` and
`test_blocker_names_both_failing_tests` fails.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, until
from gbfleet.until import _block_ci_red, ci_red_blocker, failed_test_summary

HEAD = "abcdef1234567890"
LOG = (
    "2026-11-09T10:00:00.0000000Z \x1b[31mFAILED\x1b[0m tests/x.py::t1 - AssertionError: one\n"
    "2026-11-09T10:00:01.0000000Z FAILED tests/y.py::t2 - ResumeFailed: Committer identity unknown\n"
    "2026-11-09T10:00:02.0000000Z ===== 2 failed, 40 passed in 3.2s =====\n"
)


def _cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _forge(monkeypatch, log: str | None, api_err: str = "HTTP 403"):
    rollup = {"statusCheckRollup": [
        {"name": "ok", "conclusion": "SUCCESS", "detailsUrl": "https://g/runs/1/job/1"},
        {"name": "fleet", "conclusion": "FAILURE", "detailsUrl": "https://g/runs/9/job/77"},
    ]}
    calls = []

    def fake(repo, *args, **kw):
        calls.append(args)
        if args[0] == "pr":
            return _cp(json.dumps(rollup))
        if log is None:
            return _cp(rc=1, err=api_err)
        return _cp(log)

    monkeypatch.setattr(propose, "_gh", fake)
    return calls


class _Planner:
    def __init__(self):
        self.calls = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        return {}


def test_blocker_names_both_failing_tests(monkeypatch):
    calls = _forge(monkeypatch, LOG)
    planner = _Planner()
    assert _block_ci_red(planner, "GRPH-1", HEAD, "fleet", "", repo=Path("."), branch="gb/x")
    note = planner.calls[0][1]["blocker"]
    assert "tests/x.py::t1 - AssertionError: one" in note
    assert "tests/y.py::t2 - ResumeFailed: Committer identity unknown" in note
    assert "2 failed, 40 passed" in note
    assert "\x1b" not in note
    assert any(a[:2] == ("api", "repos/{owner}/{repo}/actions/jobs/77/logs") for a in calls)


def test_unreadable_log_says_so(monkeypatch):
    _forge(monkeypatch, None)
    out = failed_test_summary(Path("."), "gb/x")
    assert "fleet: log could not be read" in out and "HTTP 403" in out


def test_log_without_failed_lines_is_not_silent(monkeypatch):
    _forge(monkeypatch, "all good\n")
    assert "no pytest FAILED lines" in failed_test_summary(Path("."), "gb/x")


def test_summary_is_capped(monkeypatch):
    log = "".join(f"FAILED tests/a.py::t{i} - {'x' * 400}\n" for i in range(30))
    _forge(monkeypatch, log)
    out = failed_test_summary(Path("."), "gb/x")
    assert len(out.encode()) <= 2048 + 20
    assert out.count("FAILED tests/a.py") <= 10


def test_blocker_without_tests_is_the_old_text():
    assert ci_red_blocker(HEAD, "fleet") == (
        f"CI red at {HEAD[:12]}: fleet failed. Fix it on the branch, then move the "
        "item back to next (gbfleet held review: a reviewer could not sign off).")
    assert until.ci_red_blocker(HEAD, "fleet", "T").endswith("Failing tests:\nT")
