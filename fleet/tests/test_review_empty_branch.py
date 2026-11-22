"""GRPH-1843 — an empty review branch does not keep holding its areas.

Sabotage for the first test: restore `return True` at the top of
`_renew_review_hold`. The hold is renewed unconditionally, the bounce never
happens, and this test fails.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import propose as propose_mod
from gbfleet import until
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _capture(monkeypatch):
    from gbfleet import observe

    events: list[tuple[str, dict]] = []
    real = observe.emit

    def capture(event, **fields):
        events.append((event, dict(fields)))
        return real(event, **fields)

    monkeypatch.setattr(observe, "emit", capture)
    return events


def test_an_empty_review_branch_releases_its_areas_in_one_cycle(
        git_repo, tmp_path, scripts, state, monkeypatch):
    """After one cycle the empty branch is bounced, the event names it and
    "no commits", and the areas it held are free."""
    _git(git_repo, "branch", "gb/empty")
    events = _capture(monkeypatch)
    review = [{
        "id": "GRPH-1377",
        "status": "review",
        "branch": "gb/empty",
        "line_id": "GRPH-L1",
        "touchpoints": ["fleet/src/gbfleet/until.py"],
        "evidence": [],
        "review_claimed_by": "",
        "claimed_by": "",
    }]
    held = {"on": True}
    seen: list[dict] = []
    bounces: list[dict] = []
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, review=review, clusters=1,
                                   cluster_items=[["GRPH-1377"]])
    real = planner.call

    def call(tool, **kw):
        if tool == "collision_clusters":
            if held["on"]:
                payload = {"clusters": [{
                    "items": ["GRPH-1377"],
                    "held_by": ["review"],
                    "held_because": [{
                        "by": "GRPH-1377",
                        "reserved": "review",
                        "held": ["GRPH-1842"],
                    }],
                }], "total": 1}
            else:
                payload = {"clusters": [{"items": ["GRPH-1842"]}], "total": 1}
            seen.append(payload)
            return payload
        if tool == "bounce":
            bounces.append(dict(kw))
            held["on"] = False
            review.clear()
            return {"id": kw.get("id"), "status": "next"}
        return real(tool, **kw)

    planner.call = call
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=until.Limits(max_workers=0),
    )
    assert result.reason == "idle", result.as_json()
    assert bounces and "GRPH-1377" in bounces[0]["reason"]
    assert "no commits" in bounces[0]["reason"]
    assert seen, "collision_clusters was never read after the release"
    last = seen[-1]["clusters"][0]
    assert not last.get("held_by")
    assert last.get("items") == ["GRPH-1842"]
    assert not any(h.get("held") for h in last.get("held_because") or [])
    details = [str(fields.get("detail") or "") for _name, fields in events]
    assert any("GRPH-1377" in detail and "no commits" in detail for detail in details)


def test_a_branch_with_commits_keeps_the_hold(git_repo):
    _git(git_repo, "checkout", "-q", "-b", "gb/real")
    (git_repo / "f.txt").write_text("y\n", encoding="utf-8")
    _git(git_repo, "add", "f.txt")
    _git(git_repo, "commit", "-qm", "work")
    _git(git_repo, "checkout", "-q", "main")
    row = {"id": "GRPH-1", "branch": "gb/real"}
    assert until._renew_review_hold(row, git_repo, "main") is True
    assert until._empty_branch_reason(row, git_repo, "main") == ""


def test_an_unreadable_branch_is_not_treated_as_empty(git_repo):
    row = {"id": "GRPH-1", "branch": "gb/not-fetched"}
    assert until._renew_review_hold(row, git_repo, "main") is True
    assert until._empty_branch_reason(row, git_repo, "main") == ""


def test_nothing_to_commit_is_a_planner_decision_not_a_bounce(git_repo, monkeypatch):
    _git(git_repo, "branch", "gb/empty")
    events = _capture(monkeypatch)

    class _P:
        def __init__(self):
            self.calls: list[tuple] = []

        def call(self, tool, **kw):
            self.calls.append((tool, kw))
            return {"id": kw.get("id"), "status": kw.get("status") or "blocked"}

    planner = _P()
    row = {
        "id": "GRPH-1377",
        "branch": "gb/empty",
        "evidence": [{"kind": "note",
                      "detail": "Branch is identical to base, nothing to commit"}],
    }
    kept = until._release_empty_branches(planner, [row], git_repo, "HEAD", "GRPH-P1")
    assert kept == []
    tools = [tool for tool, _kw in planner.calls]
    assert "bounce" not in tools
    updated = [kw for tool, kw in planner.calls if tool == "update_item"]
    assert updated and updated[0]["status"] == "blocked"
    blocker = updated[0]["blocker"]
    assert "duplicate" in blocker and "superseded" in blocker
    assert "no commits" in blocker and "GRPH-1377" in blocker
    details = [str(fields.get("detail") or "") for _name, fields in events]
    assert any("GRPH-1377" in detail and "no commits" in detail for detail in details)


def test_no_commits_between_releases_instead_of_waiting(monkeypatch):
    """gh's 'No commits between' is the same release, not an eternal PR wait."""
    events = _capture(monkeypatch)

    def fake_propose(*_a, **_k):
        return propose_mod.Proposed(
            branch="gb/empty",
            reason="gh pr create failed: No commits between main and gb/empty",
        )

    monkeypatch.setattr(propose_mod, "propose", fake_propose)
    monkeypatch.setattr(propose_mod, "title_subject", lambda *_a, **_k: "subj")
    monkeypatch.setattr(propose_mod, "describe", lambda *_a, **_k: ("t", "b"))

    class _P:
        def __init__(self):
            self.calls: list[tuple] = []

        def call(self, tool, **kw):
            self.calls.append((tool, kw))
            return {"id": kw.get("id"), "status": "next"}

    planner = _P()
    row = {"id": "GRPH-1377", "branch": "gb/empty"}
    ready, waiting = until._pr_opened(
        [row], Path("/tmp/does-not-matter"), "main", set(), planner=planner,
        agent_id="GRPH-P1")
    assert ready == [] and waiting == []
    assert [tool for tool, _kw in planner.calls] == ["bounce"]
    assert "no commits" in planner.calls[0][1]["reason"].lower()
    assert "GRPH-1377" in planner.calls[0][1]["reason"]
    names = [name for name, _fields in events]
    assert "review_waits_on_pr" not in names
    assert "review_empty_branch" in names


def test_a_transient_pr_failure_still_waits(monkeypatch):
    events = _capture(monkeypatch)

    def fake_propose(*_a, **_k):
        return propose_mod.Proposed(branch="gb/x", reason="gh pr create failed: HTTP 502")

    monkeypatch.setattr(propose_mod, "propose", fake_propose)
    monkeypatch.setattr(propose_mod, "title_subject", lambda *_a, **_k: "subj")
    monkeypatch.setattr(propose_mod, "describe", lambda *_a, **_k: ("t", "b"))

    class _P:
        def call(self, tool, **kw):
            raise AssertionError(f"an empty-branch release must not run for {tool}")

    row = {"id": "GRPH-1", "branch": "gb/x"}
    ready, waiting = until._pr_opened(
        [row], Path("/tmp/does-not-matter"), "main", set(), planner=_P())
    assert waiting == [row] and ready == []
    assert [name for name, _fields in events] == ["review_waits_on_pr"]


def test_a_held_line_item_with_acceptance_is_reported_held_by():
    """Out of the pool only because GRPH-1377 holds the area: held_by, not missing."""
    class _P:
        def call(self, tool, **kw):
            if tool == "collision_clusters":
                return {"clusters": [
                    {"items": ["GRPH-1377"], "held_by": ["review"],
                     "held_because": [{
                         "by": "GRPH-1377",
                         "held": ["GRPH-1842", "GRPH-9", "GRPH-1377"],
                     }]},
                    {"items": ["GRPH-2"], "held_by": []},
                ]}
            if kw.get("status") == "next":
                return {"results": [
                    {"id": "GRPH-1842", "line_id": "L",
                     "description": "## Acceptance\n- ok\n",
                     "touchpoints": ["until.py"]},
                    {"id": "GRPH-9", "line_id": "L",
                     "description": "no clause", "touchpoints": ["until.py"]},
                    {"id": "GRPH-2", "line_id": "L",
                     "description": "## Acceptance\n- ok\n",
                     "touchpoints": ["a.py"]},
                    {"id": "GRPH-1", "line_id": "L",
                     "description": "## Acceptance\n- ok\n",
                     "touchpoints": ["a.py"]},
                ]}
            return {"results": []}

    got = {row["id"]: row for row in until._unready(_P(), "L")}
    assert got["GRPH-1842"] == {"id": "GRPH-1842", "held_by": "GRPH-1377"}
    assert "missing" not in got["GRPH-1842"]
    assert got["GRPH-9"] == {"id": "GRPH-9", "missing": ["acceptance"]}
    assert "GRPH-2" not in got
    assert "GRPH-1" not in got
    assert "GRPH-1377" not in got
