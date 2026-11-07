"""The drain holds a reviewer spawn until CI has finished at the head (GRPH-1392).

Sabotage: make `_ci_attested` return `rows` unchanged and the pending test fails.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet import until
from gbfleet.until import _ci_attested

ROW = {"id": "GRPH-1", "branch": "gb/x"}


def _verdict(monkeypatch, state, head="abcdef1234567890", job=""):
    monkeypatch.setattr(until, "ci_verdict", lambda *a: (state, head, job))


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: seen.append((ev, kw)))
    return seen


def test_pending_ci_holds_the_spawn_and_logs_the_commit(monkeypatch):
    _verdict(monkeypatch, "pending")
    seen = _events(monkeypatch)
    logged: set = set()
    assert _ci_attested([ROW], Path("."), logged) == []
    assert _ci_attested([ROW], Path("."), logged) == []
    assert [e for e, _ in seen] == ["review_waits_on_ci"]  # once per head
    assert seen[0][1]["commit"] == "abcdef1234567890"


def test_green_ci_spawns(monkeypatch):
    _verdict(monkeypatch, "green")
    seen = _events(monkeypatch)
    assert _ci_attested([ROW], Path("."), set()) == [ROW]
    assert seen == []


def test_red_ci_names_the_failing_job(monkeypatch):
    _verdict(monkeypatch, "red", job="fleet-tests")
    seen = _events(monkeypatch)
    assert _ci_attested([ROW], Path("."), set()) == [ROW]
    assert seen[0][0] == "review_ci_red" and seen[0][1]["job"] == "fleet-tests"


def test_unknown_ci_does_not_wedge_the_queue(monkeypatch):
    _verdict(monkeypatch, "unknown", head="")
    assert _ci_attested([ROW], Path("."), set()) == [ROW]
