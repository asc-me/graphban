"""The local stdio surface. PRD-22 S3 and D-a.

Two servers, and only one of them has authority. `gbfleet` runs on the developer's
machine and the Graphban server never learns its calls happened. The tests that matter
here are about the SHAPE of that surface — there are no HTTP routes on it, no HTTP
status codes in it, and a tool that fails is a successful exchange carrying `isError`
rather than a transport failure, because the planner has to be able to tell "your
adapter is broken" from "the supervisor is gone".
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gbfleet import mcp
from gbfleet.mcp import METHOD_NOT_FOUND, PARSE_ERROR, TOOLS, Fleet, handle, serve
from gbfleet.spawn import Reason
from gbfleet.worktree import create, reap

from tests.test_supervisor import _factory, _seats, _server


@pytest.fixture
def fleet(git_repo: Path, tmp_path: Path, scripts, state: Path) -> Fleet:
    workspace = tmp_path / "ws"
    return Fleet(
        repo=git_repo,
        workspace=workspace,
        client=_server(workspace),
        launch_for=lambda name, model="", tuning=None: _factory(scripts, "works_then_waits", adapter=name),
    )


def _call(fleet: Fleet, tool: str, **args) -> dict:
    reply = handle(fleet, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    })
    return reply["result"]


def _value(result: dict) -> dict:
    return result["structuredContent"]


# --- the shape of the surface --------------------------------------------------------


def test_there_are_no_http_routes_or_status_codes_anywhere_in_it():
    """The grill misread this three times, asking for the endpoint URLs by which the
    supervisor would invoke `spawn` on the Graphban server. There are none, and the
    module has to keep being able to say so."""
    source = Path(mcp.__file__).read_text(encoding="utf-8")
    for banned in ("fastapi", "flask", "uvicorn", "@app.", "@router.", "status_code"):
        assert banned not in source, f"the local surface reached for {banned}"


def test_it_offers_exactly_the_four_tools_the_prd_names():
    assert [t["name"] for t in TOOLS] == ["spawn", "stop", "ps", "orphans"]


def test_spawn_takes_no_count(fleet: Fleet):
    """The decision, pinned.

    The planner holds BOTH servers, so it can read `collision_clusters` and
    `get_backlog` itself, decide how many to run, mint that many seats and call this
    once each. A `count` here would put that decision in the component that cannot see
    the work — and `propose_allocation` returns zero before any child exists, so the
    supervisor could not answer it honestly even if asked.
    """
    spawn = next(t for t in TOOLS if t["name"] == "spawn")
    properties = spawn["inputSchema"]["properties"]
    for counting in ("count", "n", "workers", "how_many"):
        assert counting not in properties
    assert set(spawn["inputSchema"]["required"]) == {"adapter", "enrolment_code"}


def test_initialize_answers_without_a_credential(fleet: Fleet):
    """Authentication is process ownership — the planner speaks over a pipe to a child
    it launched. There is nothing on this surface for a credential to protect."""
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert reply["result"]["serverInfo"]["name"] == "gbfleet"
    assert "capabilities" in reply["result"]


def test_a_notification_gets_no_reply(fleet: Fleet):
    assert handle(fleet, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# --- errors are tool results, not transport failures ---------------------------------


def test_a_failing_tool_is_a_successful_exchange_carrying_isError(
    fleet: Fleet, tmp_path: Path
):
    """D-a. A transport failure says "the supervisor is gone" when it means "your
    adapter is broken", and the planner acts very differently on those two."""
    fleet.launch_for = lambda name, model="", tuning=None: (_ for _ in ()).throw(
        __import__("gbfleet.adapters", fromlist=["AdapterError"]).AdapterError(
            "adapter 'codex' is not implemented"
        )
    )
    result = _call(fleet, "spawn", adapter="codex", enrolment_code="WORKER-1")

    assert result["isError"] is True
    assert "codex" in result["content"][0]["text"]
    assert "error" not in result, "a tool failure must not surface as a JSON-RPC error"


def test_a_missing_argument_is_a_tool_error_not_a_crash(fleet: Fleet):
    result = _call(fleet, "spawn", adapter="claude")
    assert result["isError"] is True
    assert "enrolment_code" in result["content"][0]["text"]


def test_an_unknown_tool_is_a_protocol_error(fleet: Fleet):
    """The other side of the same line: the tool does not exist, so no tool failed."""
    reply = handle(fleet, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sign_off", "arguments": {}},
    })
    assert reply["error"]["code"] == METHOD_NOT_FOUND
    assert "sign_off" in reply["error"]["message"]


def test_an_unknown_method_is_a_protocol_error(fleet: Fleet):
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert reply["error"]["code"] == METHOD_NOT_FOUND


def test_unparseable_input_does_not_take_the_server_down(fleet: Fleet):
    """A planner that writes a broken line should get an answer and keep going. A
    supervisor that exits on one bad message takes its whole fleet's supervision with
    it."""
    out = io.StringIO()
    serve(fleet, stdin=io.StringIO('not json\n{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'), stdout=out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == PARSE_ERROR
    assert replies[1]["id"] == 7, "the server stopped reading after one bad line"


# --- the tools ------------------------------------------------------------------------


def test_spawn_starts_one_child_and_returns_its_identity(fleet: Fleet):
    value = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-1"))
    try:
        assert value["agent_id"]
        assert value["running"] is True
        assert value["branch"].startswith("gb/")
        assert value["registration_latency"] is not None
        assert len(fleet.children) == 1
    finally:
        _call(fleet, "stop", agent_id=value["agent_id"])


def test_two_spawns_get_two_worktrees(fleet: Fleet):
    """One worker, one worktree, one branch — and the slot counter has to advance, or
    the second child collides with the first and `create` refuses."""
    first = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-1"))
    second = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-2"))
    try:
        assert first["branch"] != second["branch"]
        assert first["worktree"] != second["worktree"]
    finally:
        for child in (first, second):
            _call(fleet, "stop", agent_id=child["agent_id"])


def test_ps_lists_children_including_ones_that_have_stopped(fleet: Fleet):
    """A child that exited is still something the planner needs to see. Dropping it
    would make "we started four" and "four are running" the same answer."""
    value = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-1"))
    _call(fleet, "stop", agent_id=value["agent_id"])

    listing = _value(_call(fleet, "ps"))
    assert len(listing["children"]) == 1
    assert listing["running"] == 0
    assert listing["children"][0]["stopped_because"] == Reason.ASKED.value


def test_stopping_something_unknown_is_not_an_error(fleet: Fleet):
    """`stop` is idempotent because D-d gives revocation two paths to it — the planner
    noticing, and the supervisor's backstop poll. Two paths to one transition is fine
    only if the second is harmless."""
    value = _value(_call(fleet, "stop", agent_id="GRPH-A999"))
    assert value["stopped"] is False
    assert "no such child" in value["reason"]


def test_stopping_twice_is_not_an_error(fleet: Fleet):
    value = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-1"))
    assert _value(_call(fleet, "stop", agent_id=value["agent_id"]))["stopped"] is True
    assert _value(_call(fleet, "stop", agent_id=value["agent_id"]))["stopped"] is True


def test_stop_cleans_up_nothing(fleet: Fleet):
    """Every path into `stop` is one where something already went wrong, and tidying at
    that moment is how uncommitted work disappears. Salvage happens at reap."""
    value = _value(_call(fleet, "spawn", adapter="claude", enrolment_code="WORKER-1"))
    worktree = Path(value["worktree"])
    seat = fleet.children[0].seat_path
    instruction = worktree / ".gbfleet-instruction"
    assert seat.exists() and instruction.exists()

    _call(fleet, "stop", agent_id=value["agent_id"])

    # Asserted on what the SUPERVISOR wrote, not on the child's own output — that is
    # racy, and it is also not what `stop` would be tempted to tidy. These three are.
    assert worktree.exists(), "stop removed the worktree"
    assert seat.exists(), "stop removed the seat file"
    assert instruction.exists(), "stop removed the instruction file"


def test_orphans_lists_salvaged_branches_and_says_which_are_salvaged(
    fleet: Fleet, tmp_path: Path
):
    """Mechanical and complete. What is deliberately absent is any judgement about
    whether a half-finished diff is worth resuming — resuming an item another agent has
    already rebuilt is how two divergent solutions appear."""
    tree = create(fleet.repo, tmp_path / "dead", "wave-9", "9")
    (tree.path / "left-behind.py").write_text("x\n", encoding="utf-8")
    reap(tree)

    listing = _value(_call(fleet, "orphans"))
    row = next(o for o in listing["orphans"] if o["branch"] == tree.branch)
    assert row["salvaged"] is True
    assert row["commit"]


def test_the_supervisor_does_not_decide_whether_to_resume(fleet: Fleet, tmp_path: Path):
    """`orphans` reports; it offers no resume, no delete, no ranking. The planner
    decides, and there is nothing here for it to defer to."""
    assert [t["name"] for t in TOOLS] == ["spawn", "stop", "ps", "orphans"]
    orphans = next(t for t in TOOLS if t["name"] == "orphans")
    assert orphans["inputSchema"]["properties"] == {}
