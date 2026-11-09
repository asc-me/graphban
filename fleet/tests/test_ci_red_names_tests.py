"""GRPH-1458: a red-CI blocker names the failing tests, read from the job log on the planner.

Sabotage: drop the `propose._gh(repo, "api", ...)` log fetch in `failed_test_summary` and
`test_blocker_names_both_failing_tests` and `test_reopen_note_names_both_failing_tests` fail.
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


# --- merge.reopen_on_red: the reopened item's next child sees the failing tests -------------

REOPEN = ("merge.reopen_on_red: CI failed suite_green at the signed-off head "
          f"{HEAD[:12]} — failing check: failing jobs: fleet, windows; run: https://g/runs/9")


class _Ledger:
    """A planner whose item store answers get_item_details the way the server does."""

    def __init__(self, item: dict):
        self.item = {"evidence": [], **item}
        self.writes = []

    def call(self, tool, /, **args):
        if tool == "get_item_details":
            return dict(self.item, evidence=list(self.item["evidence"]))
        if tool == "update_item":
            self.writes.append(args)
            self.item["evidence"] += args.get("evidence") or []
            return dict(self.item)
        raise AssertionError(tool)


def _reopened(**extra):
    return _Ledger({"id": "GRPH-9", "status": "next", "branch": "gb/x",
                    "bounce_reason": REOPEN, **extra})


def test_reopen_note_names_both_failing_tests(monkeypatch):
    calls = _forge(monkeypatch, LOG)
    ledger = _reopened()
    note = until.name_red_tests(ledger, Path("."), "GRPH-9", "GRPH-A1")
    assert note is not None
    assert "tests/x.py::t1 - AssertionError: one" in note
    assert "tests/y.py::t2 - ResumeFailed: Committer identity unknown" in note
    assert ledger.writes[0]["agent_id"] == "GRPH-A1"
    assert any(a[:2] == ("api", "repos/{owner}/{repo}/actions/jobs/77/logs") for a in calls)


def test_next_childs_read_carries_the_failing_test_ids(monkeypatch):
    """What the resumed child reads with get_item_details names the node ids, not just jobs."""
    _forge(monkeypatch, LOG)
    ledger = _reopened()
    until.name_red_tests(ledger, Path("."), "GRPH-9")
    child_view = ledger.call("get_item_details", id="GRPH-9")
    text = "\n".join(str(e.get("detail")) for e in child_view["evidence"])
    assert "tests/x.py::t1" in text and "tests/y.py::t2" in text
    assert child_view["bounce_reason"] == REOPEN


def test_reopen_unreadable_log_says_why(monkeypatch):
    _forge(monkeypatch, None, api_err="HTTP 410: logs expired")
    ledger = _reopened()
    note = until.name_red_tests(ledger, Path("."), "GRPH-9")
    assert "log could not be read" in note and "HTTP 410: logs expired" in note


def test_reopen_note_written_once_per_head(monkeypatch):
    _forge(monkeypatch, LOG)
    ledger = _reopened()
    assert until.name_red_tests(ledger, Path("."), "GRPH-9")
    assert until.name_red_tests(ledger, Path("."), "GRPH-9") is None
    assert len(ledger.writes) == 1


def test_ordinary_bounce_is_left_alone(monkeypatch):
    calls = _forge(monkeypatch, LOG)
    ledger = _reopened(bounce_reason="needs a test")
    assert until.name_red_tests(ledger, Path("."), "GRPH-9") is None
    assert not ledger.writes and not calls


def test_reopen_without_branch_says_so(monkeypatch):
    _forge(monkeypatch, LOG)
    ledger = _reopened(branch="")
    assert "no branch" in until.name_red_tests(ledger, Path("."), "GRPH-9")
