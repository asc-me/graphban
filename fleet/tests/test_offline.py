"""PRD-22 D-i — offline, the lease decides.

The instinct is for the supervisor to keep running until claimed work is finished, and
it cannot: `sign_off` and `bounce` are server acts, and leases expire server-side
because heartbeats cannot land. Worse, the unbounded version puts **two agents on one
item** the moment the partition is one-sided — the laptop is offline, the server is fine
and re-hands the item — which is the collision that clustering exists to prevent.

The stated ceiling is one lease period of useful partition tolerance, not unbounded
operation. Every test here is about that boundary and the ways it could quietly stop
binding.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from gbfleet.client import Graphban
from gbfleet.spawn import Reason
from gbfleet.supervisor import Limits, Partition, up

from tests.test_supervisor import KEY, _factory, _seats

TTL = 0.3


class Flaky:
    """A server that can be cut off and brought back mid-wave.

    Real HTTP shapes through `MockTransport`, so the client's own
    unreachable-vs-answered taxonomy is exercised rather than bypassed.
    """

    def __init__(self, workspace: Path, ttl: float = TTL, holdings: list[str] | None = None):
        self.workspace = workspace
        self.ttl = ttl
        self.holdings = holdings or []
        self.reachable = True
        self.calls = 0

    def client(self) -> Graphban:
        return Graphban("http://gb.invalid", KEY, transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if not self.reachable:
            raise httpx.ConnectError("no route to host")

        body = json.loads(request.content)
        tool = body["params"]["name"]
        if tool == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [],
                       "rationale": "no agents online — nothing to allocate"}
        else:
            trees = sorted(
                p for p in self.workspace.glob("*") if p.is_dir() and p.name != "logs"
            )
            payload = {
                "agents": [
                    {
                        "id": f"GRPH-A{i + 1}",
                        "worktree": str(p),
                        "state": "working",
                        "enrolled": True,
                        "enrolment_id": f"seat-{i + 1}",
                        "holdings": [{"id": h} for h in self.holdings],
                    }
                    for i, p in enumerate(trees)
                ],
                "presence_ttl_seconds": self.ttl,
                "heartbeat_interval_seconds": self.ttl / 3,
            }
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload},
        })


def _bounded(limit: int = 200):
    """A sleep that gives up rather than hanging.

    Every test here uses a child that outlives the wave, so a mutation removing the
    lease check leaves nothing to end the loop. A hanging test is not a failing test —
    it times out somewhere far away and reports a timeout, not a defect. Two sabotages
    hung here before this existed.
    """
    seen = {"n": 0}

    def sleep(seconds: float) -> None:
        seen["n"] += 1
        if seen["n"] > limit:
            raise AssertionError(
                f"the wait loop polled {limit} times without ending — nothing is "
                "enforcing the lease, and the children would run forever"
            )
        time.sleep(seconds)

    return sleep


def _cut_off_after(server: Flaky, rosters: int):
    """Let the wave get going, then take the network away.

    Counts `fleet_status` calls specifically. Counting every call would make the test
    depend on how many times the supervisor happens to ask `propose_allocation`, which
    is not what it is about.
    """
    original = server._handle
    seen = {"n": 0}

    def handle(request):
        if json.loads(request.content)["params"]["name"] == "fleet_status":
            seen["n"] += 1
            if seen["n"] > rosters:
                server.reachable = False
        return original(request)

    return handle


# --- the ceiling is the server's number, not ours ----------------------------------


def test_the_ceiling_comes_from_the_server_not_a_constant(git_repo: Path, tmp_path: Path, scripts, state: Path):
    """`presence_ttl_seconds` is the honest number.

    D-i names `lease_seconds`, which is known at CLAIM by the child and which the
    supervisor never sees. What it is given, on every `fleet_status`, is the presence
    TTL — and that is the number that actually decides: past it an agent reads offline
    and its item leases lapse into the queue.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=99.0)
    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), server.client(),
        state=state, workspace=workspace, poll=0.02,
    )
    assert wave.partition.ceiling == 99.0


def test_a_ceiling_we_never_learned_is_not_treated_as_no_ceiling():
    """The absence-reads-clean defect aimed at the one decision this makes.

    `None` means the server never told us its presence TTL. Reading that as "unbounded"
    would let children run past the point the server re-hands their work, which is the
    exact two-agents-on-one-item collision D-i exists to prevent.
    """
    unknown = Partition()
    assert unknown.ceiling is None
    assert "never learned" in unknown.describe()
    assert "unbounded" not in unknown.describe().lower()


# --- the boundary --------------------------------------------------------------------


def test_a_short_partition_does_not_stop_anyone(git_repo: Path, tmp_path: Path, scripts, state: Path):
    """Until a worker's lease expires the server will not give its item to anyone else,
    so a child that cannot reach the server may keep building. That is not optimism; it
    is what the lease promises, and stopping early throws work away for nothing.

    The server genuinely goes away here and comes back. An earlier version of this test
    never partitioned at all, so `_enforce_the_lease` returned before doing anything and
    a sabotage that stopped children on the FIRST missed poll passed it.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=600.0)
    calls = {"n": 0}
    original = server._handle

    def handle(request):
        if json.loads(request.content)["params"]["name"] == "fleet_status":
            calls["n"] += 1
            server.reachable = not (3 <= calls["n"] <= 5)
        return original(request)

    server._handle = handle

    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_waits"), server.client(),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.partition.longest > 0, "the server never actually went away"
    assert wave.partition.reached_ceiling is False
    assert wave.spawned[0].stopped_because is None, (
        "a partition well inside the ceiling stopped a child, throwing away work the "
        "lease still protected"
    )


def test_past_the_ceiling_every_running_child_is_stopped(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """Past one presence TTL the server has requeued the work and a second agent may
    already hold it. The claim is gone either way; continuing only risks two divergent
    solutions to the same item."""
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=TTL)
    server._handle = _cut_off_after(server, rosters=2)

    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"), server.client(),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.partition.reached_ceiling is True
    assert wave.spawned[0].stopped_because is Reason.LEASE_LAPSED
    assert any("presence TTL" in f for f in wave.failures)


def test_the_work_survives_the_claim_not_the_other_way_round(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """D-i: "The work survives; the claim does not."

    Stopping leaves worktree and branch intact — killing cleans up nothing — and the
    reap that follows salvages the diff onto the child's own branch. A partition must
    not cost the work.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=TTL)
    server._handle = _cut_off_after(server, rosters=2)

    wave = up(
        git_repo, _seats(1), _factory(scripts, "writes_then_sleeps"), server.client(),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.spawned[0].stopped_because is Reason.LEASE_LAPSED
    reaped = wave.reaped[0]
    assert reaped.salvage and reaped.salvage.commit, "the diff was not salvaged"

    import subprocess

    kept = subprocess.run(
        ["git", "show", f"{reaped.branch}:half-done.py"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "half a thought" in kept


def test_a_ceiling_that_was_never_learned_stops_rather_than_running_on(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The dangerous case, made safe in the dangerous direction.

    If the server answered `propose_allocation` and then vanished before ever reporting
    a presence TTL, the supervisor has no number to bound anything with. Running on
    would be unbounded operation, which is the one thing D-i refuses.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=TTL)
    seen: dict[str, int] = {"ttl_reads": 0}
    original = server._handle

    def handle(request):
        body = json.loads(request.content)
        if body["params"]["name"] == "fleet_status":
            seen["ttl_reads"] += 1
            if seen["ttl_reads"] > 2:
                server.reachable = False
        return original(request)

    server._handle = handle
    server.ttl = 0  # never a usable TTL, so the ceiling is never learned

    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"), server.client(),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.partition.ceiling is None
    assert wave.spawned[0].stopped_because is Reason.LEASE_LAPSED
    assert any("never learned" in f for f in wave.failures)


# --- reconnect ------------------------------------------------------------------------


def test_work_reclaimed_during_a_partition_is_reported_not_replayed(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """On reconnect nothing is replayed blind.

    The supervisor compares what its children were known to hold against what they hold
    now, and REPORTS the difference. It does not re-submit a transition for work the
    server has already re-handed — that is the blind replay D-i forbids, and §4 means it
    could not submit one anyway.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=600.0, holdings=["GRPH-77"])

    calls = {"n": 0}
    original = server._handle

    def handle(request):
        body = json.loads(request.content)
        if body["params"]["name"] == "fleet_status":
            calls["n"] += 1
            # A real partition: the server goes away holding GRPH-77 for us, and is back
            # a moment later having given it to somebody else. Changing the holdings
            # WITHOUT a partition would prove nothing — the whole question is what was
            # true going in versus what is true coming out.
            if calls["n"] == 3:
                server.reachable = False
            elif calls["n"] >= 5:
                server.reachable = True
                server.holdings = []
        return original(request)

    server._handle = handle

    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_waits"), server.client(),
        state=state, workspace=workspace, poll=0.05,
    )

    reclaimed = wave.partition.reclaimed
    assert reclaimed, "an item was taken away and nothing said so"
    assert "GRPH-77" in next(iter(reclaimed.values()))


def test_a_partition_that_changed_nothing_reports_nothing(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The control, and it has to PARTITION to be one.

    A partition happens and the agent comes back still holding what it held. Nothing
    was reclaimed, so nothing is reported. The earlier version of this test never went
    offline, so the comparison never ran and a sabotage reporting every held item as
    reclaimed passed it — the assertion was right and could not fail.
    """
    workspace = tmp_path / "ws"
    server = Flaky(workspace, ttl=600.0, holdings=["GRPH-77"])
    calls = {"n": 0}
    original = server._handle

    def handle(request):
        if json.loads(request.content)["params"]["name"] == "fleet_status":
            calls["n"] += 1
            server.reachable = not (3 <= calls["n"] <= 5)
        return original(request)

    server._handle = handle

    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_waits"), server.client(),
        state=state, workspace=workspace, poll=0.05, sleep=_bounded(),
    )

    assert wave.partition.longest > 0, "the server never actually went away"
    assert wave.partition.held, "nothing was ever known to be held, so nothing was compared"
    assert wave.partition.reclaimed == {}
