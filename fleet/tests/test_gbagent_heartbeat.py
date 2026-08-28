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

import stat
import threading
import time
from pathlib import Path

import pytest

from gbagent.config import VerifyConfig
from gbagent.heartbeat import FALLBACK_INTERVAL, Heartbeat
from gbagent.llm import ToolCall
from gbagent.toolset import Toolset
from conftest import make_stub_script, stub_argv  # noqa: E402
from gbfleet.client import ProtocolError, ServerUnreachable, ToolFailed


class FakeCoord:
    """Counts beats and can be told to fail in each of the ways that mean something."""

    def __init__(self, *, interval: float | None = 0.02, beat_raises=None):
        self.beats = 0
        self.cadence_calls = 0
        self.beat_ids: list[object] = []
        self._interval = interval
        self._beat_raises = beat_raises
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
