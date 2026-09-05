"""PRD-38 PR 1 (criteria 2, 4) — the supervisor's two posts, from this side of the wire.

The server's half is pinned in `backend/tests/test_harness_telemetry.py`. This is the half
that decides whether a post is made at all, what it carries, and — the part worth a test of
its own — that failing to make one costs nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from gbfleet import matrix as matrix_mod
from gbfleet.client import Graphban, ALLOWED_TOOLS
from gbfleet.mcp import Fleet, handle
from gbfleet.spawn import Child, Reason
from gbfleet.supervisor import Limits, Wave, watch_tick
from gbfleet.tiers import TierTable
from tests.test_supervisor import KEY, _factory

from conftest import telemetry_ack  # noqa: E402


def _recording_server(workspace: Path, posts: list) -> Graphban:
    """The `_server` fake plus a record of every REST post, which is the point here."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/mcp":
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": "at_1"})
        body = json.loads(request.content)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        payload = {"agents": [
            {"id": f"GRPH-A{i + 1}", "worktree": str(p), "state": "idle", "enrolled": True,
             "enrolment_id": f"seat-{i + 1}", "holdings": []}
            for i, p in enumerate(trees)]}
        if body["params"]["name"] == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [], "rationale": "none"}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload}})

    return Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS,
                    transport=httpx.MockTransport(handler))


@pytest.fixture
def recorded(git_repo: Path, tmp_path: Path, scripts, state: Path):
    workspace = tmp_path / "ws"
    posts: list = []
    fleet = Fleet(repo=git_repo, workspace=workspace,
                  client=_recording_server(workspace, posts),
                  launch_for=lambda name, model="", tuning=None: _factory(
                      scripts, "works_then_waits", adapter=name),
                  tiers=TierTable.parse(["cheap=fake:qwen-local"]))
    return fleet, posts


def _spawn(fleet: Fleet, **args) -> dict:
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "spawn", "arguments": args}})
    return reply["result"]


# ---- the launch post -------------------------------------------------------------------------

def test_spawn_posts_what_it_resolved_before_the_child_starts(recorded):
    """4. Sabotage: drop the post and the server can only ever say `unknown`."""
    fleet, posts = recorded
    out = _spawn(fleet, tier="cheap", enrolment_code="WORKER-1")
    assert not out.get("isError"), out

    assert [p[0] for p in posts] == ["/api/fleet/attempts"]
    body = posts[0][1]
    assert body["enrolment_code"] == "WORKER-1"
    assert body["adapter"] == "fake"
    # The winner is spelled the way a CHILD declares itself, because that is what the server
    # compares it against: vendor first, then the model only when one was named.
    assert body["winner"] == "fake:qwen-local"
    assert body["source"] == "flag"


def test_an_explicit_adapter_is_posted_as_explicit(recorded):
    """4. `explicit` is not a resolution the matrix made, and must not be counted as one."""
    fleet, posts = recorded
    _spawn(fleet, adapter="fake", model="named", enrolment_code="WORKER-1")
    assert posts[0][1]["source"] == "explicit"
    assert posts[0][1]["winner"] == "fake:named"


def test_a_launch_post_that_cannot_land_does_not_fail_the_spawn(git_repo: Path, tmp_path: Path,
                                                               scripts, state: Path):
    """D3. A measurement that could fail a spawn would be a worse bargain than no measurement."""
    workspace = tmp_path / "ws"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/mcp":
            raise httpx.ConnectError("no route to host")
        body = json.loads(request.content)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        payload = {"agents": [{"id": "GRPH-A1", "worktree": str(p), "state": "idle",
                               "enrolled": True, "enrolment_id": "seat-1", "holdings": []}
                              for p in trees]}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload}})

    fleet = Fleet(repo=git_repo, workspace=workspace,
                  client=Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS,
                                  transport=httpx.MockTransport(handler)),
                  launch_for=lambda name, model="", tuning=None: _factory(
                      scripts, "works_then_waits", adapter=name),
                  tiers=TierTable.parse(["cheap=fake:qwen-local"]))
    out = _spawn(fleet, tier="cheap", enrolment_code="WORKER-1")
    assert not out.get("isError"), out
    assert len(fleet.children) == 1 and fleet.children[0].running


def test_the_requested_turn_budget_is_carried_on_the_child(recorded):
    """D3. A child that stopped AT its budget reads the same as one that finished early
    unless the budget is recorded beside the turns."""
    fleet, _ = recorded
    _spawn(fleet, tier="cheap", enrolment_code="WORKER-1", turns=40)
    assert fleet.children[0].turn_budget == 40


# ---- the exit report -------------------------------------------------------------------------

class _Dead:
    """A process that has already exited, with the code the test wants."""

    def __init__(self, code: int = 0) -> None:
        self._code, self.pid = code, 4242

    def poll(self) -> int | None:
        return self._code


def _exited(adapter: str = "fake", seat_id: str | None = "seat-1", code: int = 0,
            version: str = "1.2.3") -> Child:
    return Child(adapter=adapter, worktree=Path("/tmp/wt"), branch="gb/x", base="",
                 seat_path=Path("/tmp/seat.json"), process=_Dead(code),
                 started_at=time.monotonic() - 30, log_dir=Path("/tmp/logs"),
                 binary_version=version, seat_id=seat_id, turn_budget=40)


def _tick(fleet_client, children):
    wave = Wave()
    watch_tick(wave, children, Limits(), fleet_client, debug=False)


def test_a_child_that_exited_is_reported_once(recorded):
    """2. Sabotage: report from `_reap_all` instead and a child that exits early is reported
    an hour late, or never if the supervisor dies first."""
    fleet, posts = recorded
    child = _exited()
    _tick(fleet.client, [child])
    _tick(fleet.client, [child])

    reports = [b for path, b in posts if b.get("enrolment_id")]
    assert len(reports) == 1, reports
    assert reports[0]["enrolment_id"] == "seat-1"
    assert reports[0]["binary_version"] == "1.2.3"
    assert reports[0]["turn_budget"] == 40
    assert reports[0]["wall_seconds"] >= 30
    # The ADAPTER's word for exit 0, not a word this module made up.
    assert reports[0]["exit_meaning"] == "finished"
    assert child.reported is True


def test_a_child_that_never_registered_is_not_reported(recorded):
    """D3. There is no attempt to report, and the silence is already carried elsewhere."""
    fleet, posts = recorded
    _tick(fleet.client, [_exited(seat_id=None)])
    assert [b for path, b in posts if b.get("enrolment_id")] == []


def test_a_running_child_is_not_reported(recorded):
    """2. Sabotage: drop the `running` guard and every tick posts an ending that has not
    happened."""
    class _Live(_Dead):
        def poll(self):
            return None

    fleet, posts = recorded
    child = _exited()
    child.process = _Live()
    _tick(fleet.client, [child])
    assert [b for path, b in posts if b.get("enrolment_id")] == []
    assert child.reported is False


def test_the_supervisors_own_kill_is_not_reported_as_the_vendors_verdict(recorded):
    """D3. A child stopped for running past the wall clock exits with whatever the signal
    produced, and reporting that as the harness's exit code attributes the supervisor's
    decision to the harness."""
    fleet, posts = recorded
    child = _exited(code=-15)
    child.stopped_because = Reason.WALL_CLOCK
    _tick(fleet.client, [child])
    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert report["exit_meaning"] == f"stopped: {Reason.WALL_CLOCK.value}"
