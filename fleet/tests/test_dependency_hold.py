"""A dependency reopened by red CI keeps its dependent held (GRPH-1605).

Seen on the P82 drain: GRPH-1598 was held because GRPH-1597's PR was open. CI went
red, `merge.reopen_on_red` moved 1597 back to next, and the recheck lifted the hold
— "its dependencies are in origin/main" — because the check only looks at
done-but-unmerged rows. 1597 had left that set. The PR was still open.

Sabotage: lift whenever `deps.check` returns no absent row (the old check) → the
test below fails. A reopened dependency drops out of that set, and the absence
was read as merged.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import deps
from gbfleet import propose as propose_mod
from gbfleet import until


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "one")
    return repo


UNFETCHED = "0" * 40

BRIEF = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}


def _dep(status="done"):
    return {"id": "SA-558", "title": "SA-558", "status": status, "link_types": ["dependency"],
            "evidence": [{"kind": "attestation", "commit": UNFETCHED},
                         {"kind": "url", "url": "https://github.com/o/r/pull/397"}],
            "bounce_reason": ""}


class Server:
    def __init__(self, dep):
        self.dep = dep
        self.details = {
            "SEED": {"id": "SEED", "status": "next", "brief": BRIEF, "blocker": ""},
            "SIB": {"id": "SIB", "status": "next", "brief": BRIEF, "blocker": ""},
        }
        self.writes: list[dict] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": [{"items": ["SEED", "SIB"]}]}
        if tool == "get_item_details":
            return dict(self.details[kw["id"]])
        if tool == "related_work":
            return {"results": [self.dep] if kw["id"] == "SIB" else []}
        if tool == "delegate":
            return {"enrolment_code": "WORKER-X"}
        if tool == "update_item":
            self.writes.append(kw)
            self.details[kw["id"]]["blocker"] = kw["blocker"]
            return {}
        raise AssertionError(tool)


def _hold_sibling(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(propose_mod, "view",
                        lambda *a, **kw: ({"state": "OPEN", "number": 397}, ""))
    dep = _dep("done")
    server = Server(dep)
    held: dict[str, list[str]] = {}
    delegated: set[str] = set()
    until._delegate_next(server, "GRPH-A1", "w", delegated, None,
                         repo=repo, base="main", held=held)
    assert deps.is_hold(server.details["SIB"]["blocker"])
    assert held["SIB"] == ["SA-558"]
    return repo, server, dep, held, delegated


@pytest.mark.parametrize("status", ["next", "in_progress", "review"])
def test_a_reopened_dependency_keeps_the_hold(tmp_path, monkeypatch, status):
    """THE CASE. Move the done-but-unmerged dependency back off `done`. The dependent
    stays held, and the log says it is waiting on that item's rework."""
    repo, server, dep, held, delegated = _hold_sibling(tmp_path, monkeypatch)
    dep["status"] = status
    dep["bounce_reason"] = "merge.reopen_on_red: CI failed at the signed-off head"

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    until._recheck_holds(server, held, delegated, repo, "main")

    assert "SIB" in held and held["SIB"] == ["SA-558"]
    assert "SIB" in delegated
    assert deps.is_hold(server.details["SIB"]["blocker"])
    assert "waiting on SA-558's rework" in server.details["SIB"]["blocker"]
    assert [kw for ev, kw in events if ev == "hold_lifted"] == []
    said = [kw.get("detail") or "" for ev, kw in events if ev == "delegate_held"]
    assert any("waiting on SA-558's rework" in line for line in said)
