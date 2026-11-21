"""An idle drain costs a fixed handful of calls and then stops (GRPH-1645).

The measured failure was eighteen service drains restarting onto empty or closed
PRDs and, each time, re-reading the whole project: a merge event per long-merged
item, an unscoped hold search, and an `update_item` that wrote a blocker already
on the item. These tests count the calls and the writes. A server that does not
report `total` keeps the old loop — that is what the fixtures in `test_until` are.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from gbfleet import deps, observe, service, until
from gbfleet.client import ALLOWED_TOOLS, Graphban, ToolFailed
from gbfleet.supervisor import clear_stale_blockers
from gbfleet.until import PLANNER_TOOLS, _OPEN_STATUSES, run

from tests.test_supervisor import KEY, _factory

PRD = "GRPH-P64"
HOLD = f"waiting on #397 (GRPH-9) to merge. {deps.HOLD_MARK}"
CALL_CEILING = 25


def _mcp(payload: dict, id_: int) -> httpx.Response:
    return httpx.Response(200, json={
        "jsonrpc": "2.0", "id": id_,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                   "structuredContent": payload},
    })


def _error(message: str, id_: int) -> httpx.Response:
    return httpx.Response(200, json={
        "jsonrpc": "2.0", "id": id_,
        "result": {"isError": True,
                   "content": [{"type": "text", "text": message}],
                   "structuredContent": {"error": {
                       "code": "validation", "message": message}}},
    })


def _clients(workspace: Path, calls: list, *, status: str = "approved",
             open_totals: dict | None = None, done: list | None = None,
             refuse_prd: bool = False):
    totals = {s: 0 for s in _OPEN_STATUSES}
    totals.update(open_totals or {})
    done_rows = list(done or [])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/fleet/observations":
            return httpx.Response(404, json={"detail": "no such item"})
        if request.url.path != "/api/mcp":
            return httpx.Response(200, json={"recorded": True})
        body = json.loads(request.content)
        if body.get("method") == "tools/list":
            calls.append(("tools/list", {}))
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"tools": [{"name": "delegate", "inputSchema": {
                    "properties": {"scope": {"type": "string"}}}}]},
            })
        tool = body["params"]["name"]
        args = dict(body["params"].get("arguments") or {})
        calls.append((tool, args))
        rid = body["id"]
        if tool == "get_prd":
            return _mcp({"id": PRD, "project_id": "core", "status": status}, rid)
        if tool == "get_context":
            return _mcp({"project_id": "core", "gitops": {},
                         "maintenance_line": None}, rid)
        if tool == "register_agent":
            return _mcp({"agent_id": "GRPH-P1", "active_role": "planner"}, rid)
        if tool == "collision_clusters":
            return _mcp({"clusters": [], "total": 0}, rid)
        if tool == "fleet_status":
            return _mcp({"agents": []}, rid)
        if tool == "get_rule_toggles":
            return _mcp({"library_loaded": True, "toggles": []}, rid)
        if tool == "search_items":
            if refuse_prd and "prd_id" in args:
                return _error("search_items does not take prd_id", rid)
            if args.get("query") == deps.HOLD_MARK:
                return _mcp({"results": [], "total": 0, "has_more": False}, rid)
            st = args.get("status")
            if st == "done":
                return _mcp({"results": done_rows, "total": len(done_rows),
                             "has_more": False}, rid)
            if st in totals:
                # A tag search is a typed-wait read. The open-count probe sends
                # status only, so a tagged query must not reuse that row.
                if args.get("tags"):
                    return _mcp({"results": [], "total": 0, "has_more": False}, rid)
                n = totals[st]
                row = []
                if n:
                    row = [{"id": f"{st}-1", "status": st, "prd_id": PRD,
                            "blocker": "needs a person", "tags": []}]
                return _mcp({"results": row, "total": n, "has_more": False}, rid)
            return _mcp({"results": [], "total": 0, "has_more": False}, rid)
        if tool == "get_item_details":
            return _mcp({"id": args.get("id"), "status": "done", "prd_id": PRD}, rid)
        if tool == "update_item":
            return _mcp({"id": args.get("id")}, rid)
        return _mcp({}, rid)

    transport = httpx.MockTransport(handler)
    planner = Graphban("http://gb.invalid", KEY, allowed=PLANNER_TOOLS, transport=transport)
    supervisor = Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS, transport=transport)
    return planner, supervisor


def _run(git_repo, scripts, state, workspace, planner, supervisor, **kw):
    return run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=1, prd=PRD, **kw,
    )


def _tool_calls(calls: list) -> list[tuple]:
    return [c for c in calls if c[0] != "tools/list"]


def test_an_idle_cycle_is_a_fixed_number_of_calls_whatever_done_holds(
    git_repo, tmp_path, scripts, state,
):
    """1,000 done items must not add a call. Sabotage: scan status=done on this path."""
    workspace = tmp_path / "ws"
    empty: list = []
    planner, supervisor = _clients(workspace, empty, done=[])
    first = _run(git_repo, scripts, state, workspace, planner, supervisor)
    assert first.reason == "idle", first.as_json()
    assert first.exit == 0

    full: list = []
    done = [{"id": f"GRPH-{i}", "status": "done", "prd_id": PRD} for i in range(1000)]
    planner, supervisor = _clients(workspace, full, done=done)
    second = _run(git_repo, scripts, state, workspace, planner, supervisor)
    assert second.reason == "idle"
    n_empty = len(_tool_calls(empty))
    n_full = len(_tool_calls(full))
    assert n_empty == n_full
    assert n_empty <= CALL_CEILING, _tool_calls(empty)
    assert not any(name == "update_item" for name, _ in full)
    assert not any(args.get("status") == "done" for name, args in full if name == "search_items")
    holds = [args for name, args in full
             if name == "search_items" and args.get("query") == deps.HOLD_MARK]
    assert holds and all(args.get("prd_id") == PRD for args in holds)
    bare = [args for name, args in full
            if name == "search_items" and not args.get("query")
            and not args.get("status") and not args.get("prd_id")]
    assert bare == []


def test_a_recorded_merge_is_not_said_again_on_the_next_run(
    git_repo, tmp_path, scripts, state, monkeypatch,
):
    """The first process still reports the skip. The next one emits no merge event."""
    said: list[str] = []
    real = observe.emit

    def spy(event, **fields):
        said.append(event)
        real(event, **fields)

    monkeypatch.setattr(observe, "emit", spy)
    done = [{
        "id": "GRPH-1", "status": "done", "prd_id": PRD,
        "evidence": [{"kind": "url", "commit": "abc123def456",
                      "detail": "merged by gbfleet: squash of #1 landed as abc123def456",
                      "url": "https://example.test/pull/1"}],
    }]
    workspace = tmp_path / "ws"
    first_calls: list = []
    planner, supervisor = _clients(workspace, first_calls, done=done)
    first = _run(git_repo, scripts, state, workspace, planner, supervisor, merge=True)
    assert first.reason == "idle", first.as_json()
    assert said.count("merge") == 1

    said.clear()
    second_calls: list = []
    planner, supervisor = _clients(workspace, second_calls, done=done)
    second = _run(git_repo, scripts, state, workspace, planner, supervisor, merge=True)
    assert second.reason == "idle"
    assert said.count("merge") == 0
    assert not any(name == "get_item_details" for name, _ in second_calls)


def test_a_dependency_hold_is_not_rewritten_when_its_text_would_not_change():
    """The clear pass used to lift the hold because the named item is done, and the
    next cycle wrote the same sentence back. Sabotage: drop the hold mark from
    `blocker_left_alone` and this sends update_item."""
    text = HOLD

    class Client:
        allowed = frozenset({"search_items", "update_item", "get_item_details"})
        updates: list = []

        def call(self, tool, **kwargs):
            if tool == "search_items":
                assert kwargs.get("prd_id") == PRD
                return {"results": [{
                    "id": "GRPH-8", "status": "blocked", "blocker": text,
                    "tags": [], "prd_id": PRD,
                }], "has_more": False, "total": 1}
            if tool == "get_item_details":
                return {"id": kwargs["id"], "status": "done"}
            if tool == "update_item":
                self.updates.append(kwargs)
                return {}
            raise AssertionError(tool)

    client = Client()
    assert clear_stale_blockers(client, prd=PRD) == []
    assert client.updates == []


def test_an_old_server_that_refuses_prd_id_is_filtered_here():
    class Server:
        def __init__(self):
            self.calls: list[dict] = []

        def call(self, tool, **kwargs):
            assert tool == "search_items"
            self.calls.append(kwargs)
            if "prd_id" in kwargs:
                raise ToolFailed("search_items", "validation",
                                  "search_items does not take prd_id")
            return {"results": [
                {"id": "A", "prd_id": PRD, "blocker": HOLD},
                {"id": "B", "prd_id": "OTHER", "blocker": HOLD},
            ]}

    server = Server()
    got = until._held_on_server(server, prd=PRD)
    assert [item for item, _blocker in got] == ["A"]
    assert "prd_id" in server.calls[0]
    assert "prd_id" not in server.calls[1]


def test_a_closed_prd_stops_the_service_on_the_first_cycle(
    git_repo, tmp_path, scripts, state, monkeypatch,
):
    stopped: list[str] = []
    monkeypatch.setenv("GBFLEET_SERVICE_NAME", "closed-prd")
    monkeypatch.setenv("GBFLEET_SERVICE_EVERY", "300")
    monkeypatch.setattr(service, "stop_restarting", lambda name: stopped.append(name) or "")
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(workspace, calls, status="closed")
    result = _run(git_repo, scripts, state, workspace, planner, supervisor)
    assert result.reason == "prd-closed", result.as_json()
    assert result.exit == 0
    assert stopped == ["closed-prd"]
    line = service.Status(
        name="closed-prd", kind="launchd", installed=True, running=False,
        last_exit=0, unit_path=Path("/tmp/x"), run_reason="prd-closed",
    ).line()
    assert "PRD closed; not restarting" in line
    assert "idle between runs" not in line


def test_no_open_items_stops_restarting_after_quiet_cycles(
    git_repo, tmp_path, scripts, state, monkeypatch,
):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(service, "QUIET_CYCLES", 2)
    stopped: list[str] = []
    monkeypatch.setenv("GBFLEET_SERVICE_NAME", "empty-prd")
    monkeypatch.setenv("GBFLEET_SERVICE_EVERY", "300")
    monkeypatch.setattr(service, "stop_restarting", lambda name: stopped.append(name) or "")
    workspace = tmp_path / "ws"
    reasons = []
    for _ in range(2):
        calls: list = []
        planner, supervisor = _clients(workspace, calls)
        result = _run(git_repo, scripts, state, workspace, planner, supervisor)
        reasons.append(result.reason)
    assert reasons == ["idle", "no-open-items"]
    assert stopped == ["empty-prd"]
    line = service.Status(
        name="empty-prd", kind="launchd", installed=True, running=False,
        last_exit=0, unit_path=Path("/tmp/x"), run_reason="no-open-items",
    ).line()
    assert "no open items for 2 cycles; not restarting" in line


def test_a_blocked_only_prd_keeps_restarting(
    git_repo, tmp_path, scripts, state, monkeypatch,
):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "cfg")
    stopped: list[str] = []
    monkeypatch.setenv("GBFLEET_SERVICE_NAME", "blocked-prd")
    monkeypatch.setenv("GBFLEET_SERVICE_EVERY", "300")
    monkeypatch.setattr(service, "stop_restarting", lambda name: stopped.append(name) or "")
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(
        workspace, calls, open_totals={"blocked": 1})
    result = _run(git_repo, scripts, state, workspace, planner, supervisor)
    assert result.reason == "idle", result.as_json()
    assert stopped == []


def test_stop_restarting_boots_the_launchd_job_out(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(service, "host", lambda: service.Host("launchd"))
    monkeypatch.setattr(service, "_run", lambda cmd, timeout=20.0: seen.append(list(cmd)) or (0, "ok"))
    service.stop_restarting("quiet-prd")
    assert seen[0][:2] == ["launchctl", "bootout"]
    assert seen[0][2].endswith("dev.graphban.fleet.quiet-prd")
