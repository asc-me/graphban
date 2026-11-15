"""GRPH-1669: a tool error from `propose_allocation` or `fleet_status` is a stop, not a crash.

Seen with gbfleet 0.18.21 while the box answered both reads with "internal error". `up`
ended on an uncaught `ToolFailed` traceback with no FAILED line and no wave summary, and
`gbfleet mcp`'s `spawn` killed the MCP server mid-registration and left the child it had
just started running with ppid 1, which nothing then reaped, timed or pushed.

The fakes answer through the real client over `MockTransport`, so the `isError` reply is
turned into `ToolFailed` by the same code that does it in production.
"""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import httpx

from gbfleet import until
from gbfleet.client import ALLOWED_TOOLS, Graphban, ToolFailed
from gbfleet.cli import report
from gbfleet.mcp import Fleet, handle, serve
from gbfleet.spawn import Reason
from gbfleet.supervisor import Limits, up

from conftest import telemetry_ack  # noqa: E402
from tests.test_supervisor import KEY, _factory, _seats
from tests.test_until import _clients

MESSAGE = "internal error"


def _failing(workspace: Path, *, failing: str, after: int = 0) -> Graphban:
    """A server whose `failing` tool answers `isError` once it has answered `after` times.

    Every worktree under `workspace` is on the roster until then, so a child registers.
    """
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        tool = body["params"]["name"]
        if tool == failing:
            seen["n"] += 1
            if seen["n"] > after:
                err = {"code": "internal_error", "message": MESSAGE}
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"],
                    "result": {"isError": True,
                               "content": [{"type": "text", "text": MESSAGE}],
                               "structuredContent": {"error": err}},
                })
        if tool == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [], "rationale": "none"}
        elif tool == "search_items":
            payload = {"results": []}
        else:
            trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
            payload = {"agents": [
                {"id": f"GRPH-A{i + 1}", "worktree": str(p), "state": "idle",
                 "enrolled": True, "enrolment_id": f"seat-{i + 1}", "holdings": []}
                for i, p in enumerate(trees)
            ]}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload},
        })

    return Graphban("http://gb.invalid", KEY,
                    allowed=ALLOWED_TOOLS | frozenset({"search_items"}),
                    transport=httpx.MockTransport(handler))


def _failed_lines(wave) -> list[str]:
    out = io.StringIO()
    report(wave, out=out)
    return [line for line in out.getvalue().splitlines() if line.startswith("FAILED")]


# --- up ---------------------------------------------------------------------------------


def test_up_stops_on_a_propose_allocation_tool_error_with_one_failed_line(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(2), _factory(scripts, "works_then_exits"),
        _failing(workspace, failing="propose_allocation"),
        state=state, workspace=workspace, poll=0.02,
    )

    assert not wave.ok
    assert wave.reason == "server-error"
    assert wave.spawned == [], "a child was started on an allocation the server did not give"
    lines = _failed_lines(wave)
    assert len(lines) == 1, lines
    assert "propose_allocation" in lines[0] and MESSAGE in lines[0]


def test_up_stops_on_a_fleet_status_tool_error_and_stops_its_child(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The child registers, then the roster read starts failing in the watch loop."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"),
        _failing(workspace, failing="fleet_status", after=2),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.02,
    )

    assert wave.reason == "server-error"
    lines = _failed_lines(wave)
    assert len(lines) == 1, lines
    assert "fleet_status" in lines[0] and MESSAGE in lines[0]
    [child] = wave.spawned
    assert not child.running, "the supervisor exited and left its child running"
    assert child.stopped_because is Reason.SERVER_ERROR
    assert child.reaped


def test_up_stops_the_child_whose_registration_wait_hit_the_tool_error(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"),
        _failing(workspace, failing="fleet_status"),
        limits=Limits(registration_window=5.0),
        state=state, workspace=workspace, poll=0.02,
    )

    assert wave.reason == "server-error"
    assert len(_failed_lines(wave)) == 1
    [child] = wave.spawned
    assert not child.running
    assert child.stopped_because is Reason.SERVER_ERROR
    assert child.reaped


# --- until ------------------------------------------------------------------------------


def test_until_stops_on_a_fleet_status_tool_error_with_one_failed_line(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    real = supervisor.call

    def call(tool, **arguments):
        if tool == "fleet_status":
            raise ToolFailed("fleet_status", "internal_error", MESSAGE)
        return real(tool, **arguments)

    monkeypatch.setattr(supervisor, "call", call)
    monkeypatch.setattr(supervisor, "fleet_status", lambda **a: call("fleet_status", **a))

    result = until.run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=2),
    )

    assert result.reason == "server_error" and result.exit == 1
    assert "fleet_status" in result.detail and MESSAGE in result.detail
    lines = _failed_lines(result.wave)
    assert len(lines) == 1, lines
    assert "fleet_status" in lines[0] and MESSAGE in lines[0]


def test_until_stops_and_reaps_the_children_it_started(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """A live child when the roster read fails is stopped and reaped, not left behind."""
    from gbfleet import supervisor as sup

    class Live:
        running = True
        stopped_because = None

    child = Live()
    reaped: list = []

    def fake_loop(wave, children, *a, **kw):
        children.append(child)
        raise ToolFailed("fleet_status", "internal_error", MESSAGE)

    def fake_stop(c, reason, *a, **kw):
        c.running = False
        c.stopped_because = reason

    monkeypatch.setattr(until, "_loop", fake_loop)
    monkeypatch.setattr(sup, "stop", fake_stop)
    monkeypatch.setattr(until, "_report_exits", lambda *a, **k: None)
    monkeypatch.setattr(until, "_reap_all", lambda wave, children, **kw: reaped.extend(children))
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    result = until.run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=2),
    )

    assert result.reason == "server_error"
    assert child.running is False and child.stopped_because is Reason.SERVER_ERROR
    assert reaped == [child]
    assert len(_failed_lines(result.wave)) == 1


# --- gbfleet mcp ------------------------------------------------------------------------


def _fleet(git_repo: Path, tmp_path: Path, scripts, client_for) -> Fleet:
    workspace = tmp_path / "ws"
    return Fleet(
        repo=git_repo, workspace=workspace, client=client_for(workspace),
        launch_for=lambda name, model="", tuning=None: _factory(
            scripts, "works_then_waits", adapter=name),
        limits=Limits(registration_window=5.0),
    )


def test_mcp_spawn_returns_a_tool_error_and_reaps_the_child_it_started(
    git_repo: Path, tmp_path: Path, scripts
):
    fleet = _fleet(git_repo, tmp_path, scripts,
                   lambda ws: _failing(ws, failing="fleet_status"))
    reply = handle(fleet, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "spawn",
                   "arguments": {"adapter": "claude", "enrolment_code": "WORKER-1"}},
    })

    result = reply["result"]
    assert result.get("isError") is True, result
    text = result["content"][0]["text"]
    assert "fleet_status" in text and MESSAGE in text
    [child] = fleet.children
    assert not child.running, "the child outlived the spawn that started it"
    assert child.stopped_because is Reason.SERVER_ERROR
    assert child.reaped

    # The server keeps serving.
    again = handle(fleet, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "ps", "arguments": {}}})
    assert not again["result"].get("isError"), again


def test_mcp_watch_thread_survives_a_failing_tick_and_retries(
    git_repo: Path, tmp_path: Path, scripts, monkeypatch
):
    fleet = _fleet(git_repo, tmp_path, scripts,
                   lambda ws: _failing(ws, failing="fleet_status"))
    ticks = {"n": 0}
    third = threading.Event()
    original = fleet.tick

    def counted(*, debug: bool = False):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            third.set()
        return original(debug=debug)  # raises ToolFailed from fleet_status

    monkeypatch.setattr(fleet, "tick", counted)

    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd)
    writer = os.fdopen(write_fd, "w")
    worker = threading.Thread(
        target=serve,
        kwargs={"fleet": fleet, "stdin": stdin, "stdout": io.StringIO(), "poll": 0.02},
        daemon=True,
    )
    worker.start()
    try:
        assert third.wait(timeout=5.0), (
            f"the watch thread stopped after {ticks['n']} tick(s): a tool error killed it"
        )
        watchers = [t for t in threading.enumerate() if t.name == "gbfleet-watch"]
        assert watchers and all(t.is_alive() for t in watchers)
    finally:
        writer.close()
        worker.join(timeout=5.0)
        stdin.close()
