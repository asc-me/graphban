"""PRD-22 D-d — revocation reaching a child that is still building.

`end_wave` and `retire_wave` revoke a seat while a child is mid-build, and there is no
push channel (§D-e, unchanged), so the child discovers it only on its next server call —
which a child deep in a build may not make for a long time.

Two paths, deliberately. The PLANNER is primary: it polls Graphban, sees a seat revoked,
and calls `stop` on the local surface. The SUPERVISOR backstops, because a planner that
is idle, dead, or mid-turn notifies nobody — and "end wave is a hard stop" is only true
if something is watching.

Two paths to the same transition is fine here **because `stop` is idempotent**. Two paths
to *deciding* the transition would not be, and the tests below are mostly about the
difference.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from gbfleet.mcp import Fleet, handle
from gbfleet.spawn import Reason
from gbfleet.supervisor import Limits, up

from tests.test_offline import Flaky, _bounded
from tests.test_supervisor import _factory, _seats


def _server(workspace: Path, mode: str, after: int = 2, ttl: float = 600.0):
    """Roster that disowns every agent after `after` reads, one of two ways."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool = body["params"]["name"]
        if tool == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [],
                       "rationale": "no agents online — nothing to allocate"}
        else:
            state["n"] += 1
            trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
            disowned = state["n"] > after
            agents = []
            for i, p in enumerate(trees):
                row = {"id": f"GRPH-A{i + 1}", "worktree": str(p),
                       "state": "working", "enrolled": True,
                       "enrolment_id": f"seat-{i + 1}", "holdings": []}
                if disowned and mode == "offline":
                    row["state"] = "offline"
                if disowned and mode == "vanished":
                    continue
                agents.append(row)
            payload = {"agents": agents, "presence_ttl_seconds": ttl,
                       "heartbeat_interval_seconds": ttl / 3}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload},
        })

    from gbfleet.client import Graphban

    return Graphban("http://gb.invalid", "gbk_test", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "mode, expected",
    [("offline", "offline while its process is alive"), ("vanished", "no longer lists this agent")],
)
def test_a_child_the_server_stopped_counting_is_stopped(
    git_repo: Path, tmp_path: Path, scripts, state: Path, mode: str, expected: str
):
    """Both shapes of the same observation.

    A revoked seat stops the child's heartbeats landing, so within the presence TTL the
    agent reads `offline`. A revoked credential shows the same way immediately. A
    dismissed agent vanishes outright. All of them mean the claim is gone and the process
    is spending money without one.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"), _server(workspace, mode),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.spawned[0].stopped_because is Reason.SEAT_GONE
    assert any(expected in f for f in wave.failures), wave.failures


def test_a_child_the_server_still_counts_is_left_alone(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The control. Without it the backstop could be stopping everything, and every test
    above would still pass."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_waits"),
        _server(workspace, mode="never", after=10_000),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )
    assert wave.spawned[0].stopped_because is None
    assert wave.ok, wave.failures


def test_a_partition_does_not_look_like_revocation(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The distinction that decides whether this is a safety net or a hazard.

    During a partition every agent is absent from a roster nobody could read. Killing the
    fleet because the network dropped is exactly what D-i exists to prevent — a child may
    keep building to its own lease deadline — so the backstop runs only when the roster
    was actually READ, and a lapsed partition must stop children for the lease reason,
    never this one.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=0.3)
    seen = {"n": 0}
    original = server._handle

    def handle_(request):
        if json.loads(request.content)["params"]["name"] == "fleet_status":
            seen["n"] += 1
            if seen["n"] > 2:
                server.reachable = False
        return original(request)

    server._handle = handle_

    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"), server.client(),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.spawned[0].stopped_because is Reason.LEASE_LAPSED, (
        "an unreachable server was read as the server disowning the child"
    )
    assert wave.partition.reached_ceiling is True


def test_the_planner_path_and_the_backstop_reach_the_same_place(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`stop` is idempotent, which is the whole reason two paths are allowed.

    The planner notices first and calls `stop`; the supervisor's poll notices later and
    calls it again. If the second call errored, or double-counted, or resurrected
    anything, the design would need one path and a race.
    """
    workspace = tmp_path / "ws"
    from gbfleet.client import Graphban
    from tests.test_supervisor import _server as ok_server

    fleet = Fleet(
        repo=git_repo, workspace=workspace, client=ok_server(workspace),
        launch_for=lambda name: _factory(scripts, "works_then_waits", adapter=name),
    )
    spawned = handle(fleet, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "spawn", "arguments": {"adapter": "x", "enrolment_code": "W-1"}},
    })["result"]["structuredContent"]

    def stop_it():
        return handle(fleet, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "stop", "arguments": {"agent_id": spawned["agent_id"],
                                                     "reason": "asked"}},
        })["result"]["structuredContent"]

    first = stop_it()
    second = stop_it()

    assert first["stopped"] is True
    assert second["stopped"] is True, "the second path errored, so two paths are unsafe"
    assert not fleet.children[0].running
    assert len(fleet.children) == 1, "stopping twice created a second child record"


def test_fleet_idle_is_still_not_a_reason(git_repo: Path):
    """D-d lists the cases that kill a child and `fleet_idle` is deliberately absent —
    the worker exits itself on empty (D-c). Adding a backstop is exactly the moment
    somebody would reach for it, so the guard is restated here next to the new one."""
    values = {r.value for r in Reason}
    assert "fleet_idle" not in values
    assert "seat_gone" in values


def test_a_child_that_already_exited_is_not_recorded_as_disowned(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """Exiting on empty is the NORMAL end of a worker's life (D-c), and the roster drops
    it a moment later when presence lapses — so "gone from the roster" describes the
    tidiest possible outcome just as accurately as it describes a revocation.

    Two children: one leaves immediately, one lingers to keep the loop running. When the
    server disowns both, only the living one is a finding. Without the `running` guard the
    finished worker is stopped and filed as SEAT_GONE, which is the same mislabelling the
    acceptance walk found in `await_registration` — the most ordinary thing a worker does,
    reported as a fault.
    """
    workspace = tmp_path / "ws"
    seats = _seats(2)

    def factory(seat, tree, instruction):
        # slot 1 finishes and leaves; slot 2 stays, so the wait loop keeps polling.
        which = "works_then_exits" if tree.branch.endswith("-1") else "sleeper"
        return _factory(scripts, which)(seat, tree, instruction)

    wave = up(
        git_repo, seats, factory, _server(workspace, mode="offline", after=2),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    finished, lingering = wave.spawned[0], wave.spawned[1]
    assert lingering.stopped_because is Reason.SEAT_GONE, "the living child was not caught"
    assert finished.stopped_because is None, (
        "a worker that finished and exited was recorded as disowned"
    )
    assert not any(finished.agent_id in f for f in wave.failures if finished.agent_id), (
        f"the finished worker was reported as a failure: {wave.failures}"
    )
