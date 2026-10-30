"""GRPH-1235: the total deadline binds the response-header phase too."""

import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from gbfleet.client import Graphban, _post_within


def _server(behaviour):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    conns = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)
            threading.Thread(target=behaviour, args=(c,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv, conns


def _run_bounded(url, deadline, limit):
    out = {}

    def go():
        t = time.monotonic()
        try:
            with httpx.Client(timeout=30.0) as client:
                _post_within(client, url, {}, deadline)
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc
        out["elapsed"] = time.monotonic() - t

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(limit)
    assert not th.is_alive(), "_post_within hung past its deadline"
    return out


def test_silent_server_times_out_within_deadline():
    srv, _ = _server(lambda c: time.sleep(60))
    try:
        out = _run_bounded(f"http://127.0.0.1:{srv.getsockname()[1]}/", 1, 5)
    finally:
        srv.close()
    assert isinstance(out["exc"], httpx.ReadTimeout)
    assert out["elapsed"] < 2.5


def test_dribbled_headers_time_out_within_deadline():
    def dribble(c):
        try:
            c.sendall(b"HTTP/1.1 200 OK\r\n")
            while True:
                time.sleep(0.2)
                c.sendall(b"X-A: b\r\n")
        except OSError:
            pass

    srv, _ = _server(dribble)
    try:
        out = _run_bounded(f"http://127.0.0.1:{srv.getsockname()[1]}/", 1, 5)
    finally:
        srv.close()
    assert isinstance(out["exc"], httpx.ReadTimeout)
    assert out["elapsed"] < 2.5


def test_trickling_body_times_out_within_deadline():
    """GRPH-1179: complete headers, then a body that dribbles a byte every 0.2s forever. Every
    per-read timeout is reset, so only the total deadline can end it."""
    def trickle(c):
        try:
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            while True:
                time.sleep(0.2)
                c.sendall(b"x")
        except OSError:
            pass

    srv, _ = _server(trickle)
    try:
        out = _run_bounded(f"http://127.0.0.1:{srv.getsockname()[1]}/", 1, 5)
    finally:
        srv.close()
    assert isinstance(out["exc"], httpx.ReadTimeout)
    assert out["elapsed"] < 2.5


def test_a_late_watchdog_does_not_close_the_client_after_the_call_returned(monkeypatch):
    """If the timer fires after the reply came back, it must leave the client open."""
    import gbfleet.client as client_mod

    timers = []

    class _HeldTimer:
        def __init__(self, _interval, fn):
            self.fn = fn
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass  # too late: the timer is already running

    monkeypatch.setattr(client_mod.threading, "Timer", _HeldTimer)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert _post_within(client, "http://x/", {}, 1).status_code == 200
    timers[0].fn()
    assert not client.is_closed


def _rpc_reply(handler_result):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "structuredContent": handler_result(body["params"]["name"])}})
    return handler


def test_a_client_the_watchdog_closed_after_a_reply_is_replaced():
    """The watchdog can fire between the reply returning and `cancel()`. The next call must get
    a fresh client, not httpx's RuntimeError('client has been closed')."""
    gb = Graphban(base_url="http://x", api_key="k",
                  transport=httpx.MockTransport(_rpc_reply(lambda tool: {"ok": True})))
    assert gb.call("fleet_status") == {"ok": True}
    gb._http.close()  # what a late `_expire` leaves behind
    assert gb.call("fleet_status") == {"ok": True}


class _Stop(Exception):
    pass


def test_until_survives_a_timed_out_collision_clusters_call(tmp_path, git_repo, monkeypatch):
    """Acceptance 4: one `until` iteration whose `collision_clusters` times out is logged as a
    transport failure (a watchdog retry) and the loop goes on to its next iteration."""
    import gbfleet.until as until_mod
    from gbfleet import observe
    from gbfleet import watchdog as watchdog_mod

    buf = io.StringIO()
    observe.configure(tmp_path, stream=buf)
    watchdog_mod.current = watchdog_mod.Watchdog(10_000, time.monotonic)
    monkeypatch.setattr(until_mod, "watch_tick", lambda *a, **k: None)

    clusters_calls = []

    def handler(request):
        body = json.loads(request.content)
        tool = body["params"]["name"]
        if tool == "collision_clusters":
            clusters_calls.append(tool)
            if len(clusters_calls) == 1:
                raise httpx.ReadTimeout("no complete reply within 1s", request=request)
            content = {"clusters": []}
        elif tool == "search_items":
            content = {"results": []}
        else:
            content = {"agents": []}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"structuredContent": content}})

    planner = Graphban(base_url="http://x", api_key="k", allowed=until_mod.PLANNER_TOOLS,
                       transport=httpx.MockTransport(handler))
    supervisor = Graphban(base_url="http://x", api_key="k",
                          transport=httpx.MockTransport(handler))

    def sleep(_seconds):
        if len(clusters_calls) >= 2:
            raise _Stop

    # A live child keeps the loop retrying (D-i) rather than ending the run on `cap`.
    live = SimpleNamespace(running=True, held_items=[])
    with pytest.raises(_Stop):
        until_mod._loop(
            until_mod.Wave(), [live], set(), lambda: None, git_repo, tmp_path / "ws", "w",
            "k", "http://x", lambda *a, **k: None, planner, supervisor, {"agent_id": "A-1"},
            [], until_mod.Limits(), 1.0, sleep, False, empty_ticks=3, mint_tries=1,
            mint_budget=1.0, minted_start=0,
        )

    assert len(clusters_calls) == 2, "the loop did not run another iteration after the timeout"
    retries = [json.loads(line) for line in buf.getvalue().splitlines()
               if line.startswith("{") and json.loads(line).get("event") == "retry"]
    assert any(e["tool"] == "collision_clusters" and "ReadTimeout" in e["error"]
               for e in retries), retries
