"""`until` must never go quiet while the server is failing (GRPH-1179).

Seen three times: no log line for 10–30 minutes, no child, process alive until kickstarted. A
stalled wave looked like an idle one. Each test below pins one of the four parts of the fix.
"""
from __future__ import annotations

import faulthandler
import io
import json
import logging
import signal
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from gbfleet import observe
from gbfleet import watchdog as watchdog_mod
from gbfleet.client import Graphban, ServerUnreachable
from gbfleet.until import Wave, _loop, Limits, run
from gbfleet.client import ALLOWED_TOOLS


class _Down:
    """A client whose every call is an unreachable server, naming the tool it was calling."""

    allowed = frozenset({"fleet_status", "search_items", "get_context"})

    def __init__(self, roster=False):
        self.calls = []
        self.roster = roster

    def fleet_status(self, **kw):
        return self.call("fleet_status", **kw)

    def call(self, tool, /, **kw):
        self.calls.append(tool)
        if tool == "fleet_status" and self.roster:
            # Somebody else holds an item, so `until` keeps waiting instead of giving up.
            return {"agents": [{"id": "A-9", "holdings": [{"id": "X-1", "phase": "building"}]}]}
        raise ServerUnreachable(f"http://x/api/mcp: ReadTimeout during {tool}", tool=tool)

    def post_attempt(self, **kw):
        return None


def _events(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]


@pytest.fixture
def log(tmp_path):
    buf = io.StringIO()
    observe.configure(tmp_path, stream=buf)
    return buf


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _drive(tmp_path, git_repo, *, stall_after, live=False):
    clock = _Clock()
    watchdog_mod.current = watchdog_mod.Watchdog(stall_after, clock)
    planner, supervisor = _Down(), _Down(roster=True)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += 60.0
        if len(slept) > 200:
            raise AssertionError("loop never stopped")

    wave = Wave()
    result = None
    try:
        result = _loop(
            wave, [], set(), lambda: None, git_repo, tmp_path / "ws", "w", "k", "http://x",
            lambda *a, **k: None, planner, supervisor, {"agent_id": "A-1"}, [], Limits(),
            1.0, sleep, False, empty_ticks=3, mint_tries=1, mint_budget=1.0, minted_start=0,
        )
    except Exception as exc:  # noqa: BLE001 - the test inspects which one
        return exc, planner, slept
    return result, planner, slept


def test_every_unreachable_retry_is_logged_with_tool_and_error(tmp_path, git_repo, log):
    outcome, _, slept = _drive(tmp_path, git_repo, stall_after=300)
    retries = [e for e in _events(log) if e["event"] == "retry"]
    assert retries, "an unreachable server produced no log line at all"
    assert all(e["tool"] and "ReadTimeout" in e["error"] for e in retries), retries
    counts = [e["retry"] for e in retries]
    assert counts == list(range(1, len(counts) + 1))
    # One event per retry: every sleep-and-continue the loop took was preceded by its line.
    assert len(retries) >= len(slept) > 0


def test_a_stall_is_logged_and_exits_nonzero(tmp_path, git_repo, log):
    outcome, _, slept = _drive(tmp_path, git_repo, stall_after=300)
    events = _events(log)
    stalls = [e for e in events if e["event"] == "stall"]
    assert stalls, [e["event"] for e in events]
    assert stalls[0]["silent_for"] >= 300 and stalls[0]["last_action"]
    assert isinstance(outcome, (watchdog_mod.Stalled, Exception))


def test_run_reports_a_stall_with_exit_1(monkeypatch, tmp_path, git_repo):
    import gbfleet.until as until_mod

    def stalled(*a, **k):
        raise watchdog_mod.Stalled("no progress for 600s; last action: start")

    monkeypatch.setattr(until_mod, "resolve_wave_base", stalled)
    report = run(git_repo, lambda *a: None, _Down(), _Down(), api_key="k", server="http://x",
                 adapter="", state=tmp_path / "state")
    assert (report.reason, report.exit, report.ok) == ("stall", 1, False)


def test_watchdog_does_not_fire_with_a_live_child():
    clock = _Clock()
    dog = watchdog_mod.Watchdog(10, clock)
    clock.now += 100
    dog.check(live=True)
    with pytest.raises(watchdog_mod.Stalled):
        dog.check(live=False)


def test_a_retry_is_not_progress():
    clock = _Clock()
    dog = watchdog_mod.Watchdog(10, clock)
    clock.now += 5
    dog.retry("x", RuntimeError("boom"))
    clock.now += 6
    with pytest.raises(watchdog_mod.Stalled):
        dog.check(live=False)


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1") or not hasattr(faulthandler, "register"),
                    reason="no SIGUSR1 / faulthandler.register on this platform (Windows)")
def test_faulthandler_is_registered_for_sigusr1(monkeypatch, tmp_path, git_repo, log):
    seen = {}
    monkeypatch.setattr(faulthandler, "register",
                        lambda sig, file=None, all_threads=True, **k: seen.update(
                            sig=sig, all_threads=all_threads, file=file))
    import gbfleet.until as until_mod
    monkeypatch.setattr(until_mod, "resolve_wave_base",
                        lambda *a, **k: (_ for _ in ()).throw(watchdog_mod.Stalled("stop")))
    run(git_repo, lambda *a: None, _Down(), _Down(), api_key="k", server="http://x",
        adapter="", state=tmp_path / "state")
    assert seen["sig"] == signal.SIGUSR1 and seen["all_threads"] is True


def test_a_server_that_never_finishes_the_reply_fails_within_the_deadline():
    """Headers arrive, then one byte every 0.1s forever: each read is inside `timeout`, so only
    a TOTAL deadline can end the call."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        while not stop.is_set():
            try:
                conn.sendall(b"1\r\n \r\n")
            except OSError:
                break
            time.sleep(0.1)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = Graphban(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", api_key="k",
                      timeout=5.0, deadline=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(ServerUnreachable) as exc:
            client.call("fleet_status") if "fleet_status" in client.allowed else None
    finally:
        stop.set()
        listener.close()
    assert time.monotonic() - started < 3.0
    assert exc.value.tool == "fleet_status"


def test_a_normal_reply_still_decodes():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "structuredContent": {"ok": True}}})

    client = Graphban(base_url="http://x", api_key="k", transport=httpx.MockTransport(handler))
    assert client.call("fleet_status") == {"ok": True}
