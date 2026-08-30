"""The child says it is alive while it is blocked (GRPH-496).

Presence is derived from `last_seen_at` and ONLY `heartbeat` refreshes it — not `update_item`,
not `search_code`. So a gbagent child read `offline` to the whole fleet one presence TTL after
registering (150s by default) while working perfectly well, and its item lease was not being
extended either, so another agent could be handed work it was actively doing.

**The load-bearing test here is `test_beats_land_during_a_blocking_tool_call`.** "A heartbeat
is sent" passes with a call at the top of the turn loop, and that version does nothing: the
silence comes from `run_tests`, a blocking subprocess with a 1800s timeout, and nothing in the
turn loop fires during it. Every other test in this file is satisfied by the version that does
not work.
"""
from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest

from gbagent import loop, orient
from gbagent.config import VerifyConfig
from gbagent.coord import WORKER_TOOLS, Coordinator
from gbagent.heartbeat import FALLBACK_INTERVAL, Heartbeat
from gbagent.llm import ToolCall, ToolTurn
from gbagent.orient import COORDINATION_TOOLS, ORIENTATION_TOOLS
from gbagent.toolset import Toolset
from conftest import make_stub_script, stub_argv  # noqa: E402
from gbfleet.client import Graphban, ProtocolError, ServerUnreachable, ToolFailed


class FakeCoord:
    """Counts beats and can be told to fail in each of the ways that mean something."""

    def __init__(self, *, interval: float | None = 0.02, beat_raises=None):
        self.beats = 0
        self.cadence_calls = 0
        self.beat_ids: list[object] = []
        self._interval = interval
        self._beat_raises = beat_raises
        # Pre-set. Every test below that only counts beats is satisfied by this, which is
        # the hole P30 D4 names: a coordinator constructed WITH an id is not a test of
        # "the model claimed and the next beat carried it". See
        # `test_a_claim_then_the_next_beat_carries_the_claimed_id`.
        self.item_id = "GRPH-999"
        self.agent_id = "GRPH-A9"
        self._lock = threading.Lock()

    def cadence(self) -> dict:
        self.cadence_calls += 1
        return {"heartbeat_interval_seconds": self._interval} if self._interval else {}

    def beat(self) -> dict:
        with self._lock:
            self.beats += 1
        if self._beat_raises is not None:
            raise self._beat_raises
        return {"id": self.item_id}


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    return tmp_path


def _slow_toolset(root: Path, seconds: float) -> Toolset:
    """A toolset whose `run_tests` blocks — the thing the turn loop cannot fire during."""
    runner = make_stub_script(root / "backend" / "slow.py", sleep=seconds,
                              prints=(f"1 passed in {seconds}s",))
    return Toolset(root=root, cfg=VerifyConfig(argv=stub_argv(runner), cwd=root / "backend",
                                               source="slow.py"))


# ---- the one that matters ---------------------------------------------------------------


def test_beats_land_during_a_blocking_tool_call(wt):
    """THE POINT OF THE THREAD. `run_tests` is a blocking subprocess; a heartbeat anywhere in
    the turn loop cannot fire while it runs, and that is exactly the window presence lapses in.

    A version that beats between turns passes every other test in this file and leaves the
    original defect in place.
    """
    coord = FakeCoord(interval=0.02)
    hb = Heartbeat(coord)
    hb.start()
    try:
        before = coord.beats
        _slow_toolset(wt, 0.4).execute(ToolCall(id="c", name="run_tests", input={}))
        during = coord.beats - before
    finally:
        hb.stop()

    assert during >= 3, (
        f"only {during} beats landed while the agent was blocked for 0.4s at a 0.02s "
        "cadence — the heartbeat is not running independently of the turn loop"
    )


# ---- the cadence comes from the server ---------------------------------------------------


def test_the_cadence_is_read_from_the_server(wt):
    """The roster's own docstring says the intervals travel with the answer because "making
    it read a constant out of documentation is how a fleet ends up with members that disagree
    about what alive means"."""
    coord = FakeCoord(interval=7.5)
    hb = Heartbeat(coord)
    hb.start()
    hb.stop()

    assert coord.cadence_calls == 1
    assert hb.interval == 7.5


def test_a_server_that_will_not_say_gets_the_fallback(wt):
    """Not fatal. A cadence nobody could ask for is what the fallback is for, and the beats
    themselves will surface a real problem soon enough."""
    coord = FakeCoord(interval=None)
    hb = Heartbeat(coord)
    hb.start()
    hb.stop()

    assert hb.interval == FALLBACK_INTERVAL


def test_an_explicit_interval_does_not_ask(wt):
    """The caller can pin it — used by the tests above, and by anything that already knows."""
    coord = FakeCoord()
    hb = Heartbeat(coord, interval=1.0)
    hb.start()
    hb.stop()

    assert coord.cadence_calls == 0 and hb.interval == 1.0


# ---- a failed beat is not one thing ------------------------------------------------------


@pytest.mark.parametrize("failure", [
    ServerUnreachable("connection reset"),
    ToolFailed("heartbeat", code="rate_limited", message="slow down"),
])
def test_a_transient_failure_does_not_declare_the_claim_gone(wt, failure):
    """The client draws this line already and says why: a 5xx or a dropped connection means
    "retrying is the right move". Killing a child over a blip would be worse than the defect
    this fixes."""
    coord = FakeCoord(interval=0.02, beat_raises=failure)
    hb = Heartbeat(coord)
    hb.start()
    time.sleep(0.1)
    hb.stop()

    assert hb.gone == "", f"a transient failure was treated as a lost claim: {hb.gone}"
    assert hb.misses > 0, "the failure was not recorded at all"


@pytest.mark.parametrize("failure, why", [
    (ProtocolError("HTTP 401: revoked"), "a 4xx — the credential is no longer accepted"),
    (ToolFailed("heartbeat", code="validation", message="no registered agent"),
     "the agent row was dismissed"),
    (ToolFailed("heartbeat", code="conflict", message="not the lease holder"),
     "somebody else holds the item"),
])
def test_a_failure_that_means_the_claim_is_gone_is_recorded_as_gone(wt, failure, why):
    """"A bad credential does not become true by waiting" — the client's words. Each of these
    means the child is building for nobody."""
    coord = FakeCoord(interval=0.02, beat_raises=failure)
    hb = Heartbeat(coord)
    hb.start()
    time.sleep(0.1)
    hb.stop()

    assert hb.gone, f"{why}: not recorded as gone"


def test_a_gone_claim_stops_the_thread_rather_than_beating_forever(wt):
    """Once the claim is gone there is nothing left to keep alive, and continuing would be
    spending requests to be told the same thing."""
    coord = FakeCoord(interval=0.02, beat_raises=ProtocolError("HTTP 401"))
    hb = Heartbeat(coord)
    hb.start()
    time.sleep(0.15)
    settled = coord.beats
    time.sleep(0.15)

    assert coord.beats == settled, "the thread kept beating after the claim was gone"
    hb.stop()


# ---- lifecycle ---------------------------------------------------------------------------


def test_stop_returns_promptly_rather_than_after_a_full_interval(wt):
    """Waits on an Event rather than sleeping. A child that has finished should not spend
    fifty seconds being tidy — and on a real cadence that is exactly what a sleep would cost."""
    coord = FakeCoord(interval=30.0)
    hb = Heartbeat(coord)
    hb.start()

    began = time.monotonic()
    hb.stop()
    elapsed = time.monotonic() - began

    assert elapsed < 1.0, f"stop() took {elapsed:.1f}s against a 30s interval"


def test_the_thread_is_a_daemon_so_it_cannot_hold_the_process_open(wt):
    """A child that has decided to exit must exit. A non-daemon thread on a 50s cadence would
    keep the process alive past its own give-up."""
    coord = FakeCoord(interval=30.0)
    hb = Heartbeat(coord)
    hb.start()
    try:
        assert hb._thread is not None and hb._thread.daemon
    finally:
        hb.stop()


# ---- and something actually starts it ----------------------------------------------------


def test_the_cli_starts_and_stops_the_heartbeat_around_the_loop():
    """A heartbeat nothing starts is the defect it was written to fix, wearing a fix's name.

    Asserted against source rather than by running the CLI, which wants a model endpoint, a
    server and a worktree. The claim here is about the WIRING — that `loop.run` is reached
    with a started heartbeat and that it is stopped on every path — and the `finally` is the
    part worth pinning: a give-up, a model outage and a clean finish all leave through it.
    """
    import inspect

    from gbagent import cli

    src = inspect.getsource(cli)
    run_at = src.index("loop.run(")
    before, after = src[:run_at], src[run_at:]

    assert "heartbeat.start()" in before, "the loop runs without a started heartbeat"
    assert "heartbeat=heartbeat" in src, "the loop is never told about it, so `gone` is unread"
    assert "heartbeat.stop()" in after, "nothing stops it"

    # In the `finally`, not on the happy path — a run that dies on ModelUnreachable must not
    # leave a thread beating for an item nobody is working.
    tail = src[src.index("finally:", run_at):]
    assert "heartbeat.stop()" in tail.split("return")[0]


# ---- P30 D4: the beat after a claim carries the claimed id, not "" ----------------------
#
# Spawned gbagent starts with `item_id=""` (`cli.py`, `--item` defaults to empty).
# `adopt()` used to run only on give-up, so a successful run's every beat was
# presence-only. The lease is 600s; `run_tests` may block 1800s. Tests that
# construct a coordinator WITH an id already (FakeCoord above, and
# `test_a_beat_carries_the_item_id_so_the_LEASE_is_extended_too`) cannot catch this.
#
# Sabotage: leave `adopt` only on give-up. This test must fail.


class _ClaimSession:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def run_turn(self, specs):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn

    def add_results(self, results):
        pass


def _mcp(payload: dict, id_: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": id_,
                                     "result": {"structuredContent": payload}})


def _claim_then_beat(wt: Path, *, tool: str, payload: dict, extra: tuple[str, ...] = ()):
    """Run a loop that claims, finishes (does NOT give up), then beat. Returns the
    heartbeat arguments sent on that beat, and the coordinator.
    """
    sent: list[dict] = []
    wanted = (*ORIENTATION_TOOLS, *COORDINATION_TOOLS, *extra)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            tools = [{"name": n, "description": n,
                      "inputSchema": {"type": "object", "properties": {}}}
                     for n in wanted]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": tools}})
        name = body["params"]["name"]
        arguments = body["params"].get("arguments") or {}
        if name == "heartbeat":
            sent.append(arguments)
            return _mcp({}, id_=body["id"])
        if name == tool:
            return _mcp(payload, id_=body["id"])
        return _mcp({}, id_=body["id"])

    allowed = WORKER_TOOLS | set(extra)
    client = Graphban("http://graphban.invalid", "gbk_seat", allowed=allowed,
                      transport=httpx.MockTransport(handler))
    coordinator = Coordinator(client=client, item_id="", agent_id="agt_9")
    orientation = orient.build(client, extra=(*COORDINATION_TOOLS, *extra))
    runner = make_stub_script(wt / "backend" / "r.py", prints=("1 passed in 1.0s",))
    toolset = Toolset(root=wt, cfg=VerifyConfig(argv=stub_argv(runner), cwd=wt / "backend",
                                                source="r.py"), orientation=orientation)
    outcome = loop.run(
        _ClaimSession([
            ToolTurn(tool_calls=[ToolCall(id="c", name=tool, input={})], wants_tools=True),
            ToolTurn(text="DONE", wants_tools=False),
        ]),
        toolset, coordinator=coordinator, window=100_000, budget=9,
    )
    assert outcome.status == "finished", (
        f"the load-bearing path is a successful claim-and-finish, not a give-up "
        f"(got {outcome.status})"
    )
    coordinator.beat()
    assert sent, "the beat after the claim never reached the server"
    return sent[-1], coordinator


def test_a_claim_then_the_next_beat_carries_the_claimed_id(wt):
    """THE LOAD-BEARING TEST for P30 D4.

    `claim_next` with `item_id=""`, then the next `beat()` carries the claimed id.
    Sabotage: leave `adopt` only on give-up; a successful finish never hits that
    path, so this fails, and the 600s lease dies under `run_tests`.
    """
    arguments, coordinator = _claim_then_beat(
        wt, tool="claim_next", extra=("claim_next",),
        payload={"claimed": True,
                 "item": {"id": "GRPH-1", "title": "a claimed item", "status": "in_progress"}},
    )

    assert coordinator.item_id == "GRPH-1"
    assert arguments.get("id") == "GRPH-1"
    assert arguments.get("agent_id") == "agt_9"


def test_a_cluster_claim_then_the_next_beat_carries_the_seed_id(wt):
    """The same, through `claim_cluster`. Remembering only `claim_next` would leave a
    fleet worker's heartbeat presence-only after the tool the PRD says to call.
    """
    arguments, coordinator = _claim_then_beat(
        wt, tool="claim_cluster",
        payload={"claimed": True,
                 "items": [{"id": "GRPH-1", "title": "seed"},
                           {"id": "GRPH-2", "title": "neighbour"}]},
    )

    assert coordinator.item_id == "GRPH-1"
    assert arguments.get("id") == "GRPH-1"


def test_an_empty_claim_leaves_the_next_beat_presence_only(wt):
    """The mirror. Remembering a phantom from `{claimed: false}` would send a lease
    beat for a row that does not exist.
    """
    arguments, coordinator = _claim_then_beat(
        wt, tool="claim_cluster",
        payload={"claimed": False, "items": []},
    )

    assert coordinator.item_id == ""
    assert "id" not in arguments
