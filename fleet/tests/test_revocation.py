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
    "mode, expected, limits",
    [
        # `offline` is only acted on once it is SUSTAINED, so this drives the bound to
        # zero. The tests below are the ones that prove the bound exists (GRPH-452).
        ("offline", "no heartbeat reached the server", Limits(registration_window=5.0,
                                                              disowned_after=0.0)),
        ("vanished", "no longer lists this agent", Limits(registration_window=5.0)),
    ],
)
def test_a_child_the_server_stopped_counting_is_stopped(
    git_repo: Path, tmp_path: Path, scripts, state: Path, mode: str, expected: str,
    limits: "Limits",
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
        limits=limits,
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
        launch_for=lambda name, model="", tuning=None: _factory(scripts, "works_then_waits", adapter=name),
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
    git_repo: Path, tmp_path: Path, scripts, log_dir: Path
):
    """Exiting on empty is the NORMAL end of a worker's life (D-c), and the roster drops
    it a moment later when presence lapses — so "gone from the roster" describes the
    tidiest possible outcome just as accurately as it describes a revocation. Without the
    `running` guard the finished worker is stopped and filed as SEAT_GONE, which is the
    same mislabelling the acceptance walk found in `await_registration`.

    **Driven directly rather than through `up`, because the integration version raced.**
    It ran one child that exits and one that lingers, and assumed the first was gone by
    the time the server disowned them. Locally it was; on a slower CI runner it was
    still alive, so the backstop stopped it — correctly — and the test failed for the one
    reason it was not about. It asserted the right property and never guaranteed the
    state it needed, which is the whole defect it was written to catch, one level up.

    Here the exited child is waited on explicitly. There is no window.
    """
    from gbfleet.spawn import Launch, spawn
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned
    from gbfleet.worktree import create

    def _child(script: str, slot: str):
        tree = create(git_repo, tmp_path / slot, "wave", slot)
        return spawn(
            Launch(adapter="fake", argv=[str(scripts["python"]), str(scripts[script])],
                   seat_path=tree.path / "mcp.json", config={"mcpServers": {}},
                   instruction=""),
            tree.path, tree.branch, log_dir / slot, base=tree.base,
        )

    finished = _child("exits_immediately", "1")
    lingering = _child("sleeper", "2")
    finished.agent_id, lingering.agent_id = "GRPH-A1", "GRPH-A2"

    finished.process.wait(timeout=30)
    assert not finished.running, "the premise: this child is genuinely gone"
    assert lingering.running, "and this one is genuinely not"

    try:
        wave = Wave()
        # The server counts neither of them any more — the shape a revocation takes.
        _catch_the_disowned(wave, [finished, lingering], {"agents": []}, Limits())

        assert lingering.stopped_because is Reason.SEAT_GONE, "the living child was not caught"
        assert finished.stopped_because is None, (
            "a worker that finished and exited was recorded as disowned"
        )
        assert len(wave.failures) == 1, wave.failures
        assert "GRPH-A2" in wave.failures[0]
    finally:
        from gbfleet.spawn import stop

        stop(lingering, Reason.SHUTDOWN)


# ---- the bound on `offline` (GRPH-452) --------------------------------------------------
#
# `offline` is derived purely from `last_seen_at`, and only `heartbeat` refreshes it. So it
# means "no heartbeat within the presence TTL" and nothing more — which a revoked child and a
# BUSY child produce identically, because a blocking tool call makes no server calls. The
# presence TTL is 150s by default and one run of this repository's own backend suite is ~9
# minutes of silence. Acting on the first reading stops healthy children for working, and
# files them as disowned.
#
# These drive `_catch_the_disowned` directly and set `offline_since` by hand, because the
# property is about elapsed time and an integration test that waited for it would take half
# an hour. The clock is the thing under test, so it is the thing supplied.


@pytest.fixture()
def quiet_child(git_repo: Path, tmp_path: Path, scripts, log_dir: Path):
    """One live child, registered, that the server has stopped counting."""
    from gbfleet.spawn import Launch, spawn, stop
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / "q", "wave", "q")
    child = spawn(
        Launch(adapter="fake", argv=[str(scripts["python"]), str(scripts["sleeper"])],
               seat_path=tree.path / "mcp.json", config={"mcpServers": {}}, instruction=""),
        tree.path, tree.branch, log_dir / "q", base=tree.base,
    )
    child.agent_id = "GRPH-A7"
    assert child.running, "the premise: this child is alive"
    try:
        yield child
    finally:
        stop(child, Reason.SHUTDOWN)


OFFLINE = {"agents": [{"id": "GRPH-A7", "state": "offline"}]}
WORKING = {"agents": [{"id": "GRPH-A7", "state": "working"}]}


def test_a_busy_child_is_not_stopped_for_being_quiet(quiet_child):
    """THE DEFECT THIS FIXES. A healthy child holding a valid seat, quiet because it is
    running the test suite, read `offline` and was stopped as disowned on the first poll."""
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned

    wave = Wave()
    _catch_the_disowned(wave, [quiet_child], OFFLINE, Limits())

    assert quiet_child.stopped_because is None, "a busy child was stopped for being quiet"
    assert quiet_child.running
    assert wave.failures == []
    assert wave.quiet == {"GRPH-A7": 0.0}, "it should be REPORTED as quiet, not acted on"


def test_a_busy_child_is_not_stopped_on_a_later_poll_within_the_bound(quiet_child):
    """THE BOUND ITSELF, which nothing above could see (GRPH-452, second bounce).

    `disowned_after` could be disabled entirely — `if quiet < limits.disowned_after` to
    `if False` — and all eleven tests in this file still passed. Not an oversight but a
    structural one: the first poll returns early at the `offline_since is None` branch,
    which starts the clock and never reads the bound. So the test above pins *the clock is
    started*, and the control below backdates past the bound and expects a stop, which it
    gets either way. Neither drives the discriminating case.

    That case is a SECOND poll while still inside the bound. Without it, a busy child is
    stopped on its second poll — the exact defect this slice was bounced for the first
    time, one poll later — and CI stays green.

    Same shape as the survivor already recorded here (a child that already exited): a
    branch the green path returns before reaching.
    """
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned

    wave = Wave()
    limits = Limits()                                             # disowned_after = 1800.0
    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)     # poll 1 starts the clock
    quiet_child.offline_since = time.monotonic() - 30.0           # 30s quiet, far inside 1800
    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)     # poll 2, still busy

    assert quiet_child.stopped_because is None, (
        "a busy child was stopped on the second poll after 30s, inside the 1800s bound")
    assert quiet_child.running
    assert wave.failures == []

    # The REPORTED figure, not just the decision. A bound that is respected while the wave
    # reports the wrong duration still misleads whoever reads the summary.
    assert wave.quiet["GRPH-A7"] == pytest.approx(30.0, abs=5.0), (
        f"the wave reports {wave.quiet.get('GRPH-A7')!r} quiet, expected about 30s")


def test_a_child_quiet_past_the_bound_is_stopped(quiet_child):
    """The control. Without it the fix above is indistinguishable from deleting the
    backstop, which would lose the revocation case D-d exists for."""
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned

    wave = Wave()
    limits = Limits(disowned_after=60.0)
    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)      # starts the clock
    quiet_child.offline_since = time.monotonic() - 61.0            # ...and it runs out
    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)

    assert quiet_child.stopped_because is Reason.SEAT_GONE
    assert len(wave.failures) == 1, wave.failures
    assert "61s" in wave.failures[0], f"the failure must say how long it was quiet: {wave.failures[0]}"
    assert "60s allowed" in wave.failures[0]
    assert "cannot tell which" in wave.failures[0], (
        "the message must not assert a cause the supervisor cannot prove"
    )
    assert "GRPH-A7" not in wave.quiet, "a stopped child should not still be listed as quiet"


def test_a_child_that_comes_back_forgets_its_quiet_spell(quiet_child):
    """Two unrelated silences must not be summed. A child that goes quiet for a long test
    run, reports in, then goes quiet again is not approaching a deadline."""
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned

    limits = Limits(disowned_after=60.0)
    wave = Wave()
    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)
    quiet_child.offline_since = time.monotonic() - 59.0    # nearly out of time

    _catch_the_disowned(wave, [quiet_child], WORKING, limits)      # ...then it heartbeats

    assert quiet_child.offline_since is None, "the quiet spell was not forgotten"
    assert "GRPH-A7" not in wave.quiet, (
        "the wave still reports a child as quiet after it started heartbeating again"
    )

    _catch_the_disowned(wave, [quiet_child], OFFLINE, limits)      # quiet again, from zero
    assert quiet_child.stopped_because is None, (
        "two unrelated quiet spells were summed into one deadline"
    )


def test_a_vanished_agent_is_stopped_at_once_with_no_grace(quiet_child):
    """The unambiguous half keeps its old behaviour, and must not inherit the bound.

    A server that is answering and does not list the id has dismissed it — there is no
    other reading, so waiting would only spend money to reach the same conclusion.
    """
    from gbfleet.supervisor import Limits, Wave, _catch_the_disowned

    wave = Wave()
    _catch_the_disowned(wave, [quiet_child], {"agents": []}, Limits(disowned_after=99_999.0))

    assert quiet_child.stopped_because is Reason.SEAT_GONE
    assert "no longer lists this agent" in wave.failures[0]
