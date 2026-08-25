"""S6 — ask the graph before reading files (PRD-24 D1, §9.3).

Two things are under test and the second is the harder one.

**The layer**: eight of the server's own tools, advertised with the SERVER's schemas, dispatched
to it, and refusing at startup if any of them has been renamed away. A declared copy of someone
else's tool contract is a thing that goes stale quietly and is discovered as a model calling
with arguments nobody accepts, at 30 seconds a turn.

**The metric**: PRD-24 §9.3 left "how is orientation cost measured?" open, and a claimed
improvement without a metric is unfalsifiable. The answer is turns-to-first-write on runs that
finished — not a count of graph calls, which measures the means, embeds its own conclusion, and
scores a run that called `code_neighbors` three times above one that grepped once and got on
with it.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from gbagent import loop, orient
from gbagent.cli import SYSTEM
from gbagent.config import VerifyConfig
from gbagent.llm import ToolCall, ToolTurn
from gbagent.orient import ORIENTATION_TOOLS, OrientationUnavailable
from gbagent.toolset import Toolset
from gbfleet.client import Graphban


def _manifest(names) -> list[dict]:
    return [{"name": n, "description": f"{n} does a thing",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}
            for n in names]


def _server(handler) -> Graphban:
    from gbagent.coord import WORKER_TOOLS
    return Graphban("http://graphban.invalid", "gbk_seat", allowed=WORKER_TOOLS,
                    transport=httpx.MockTransport(handler))


def _listing(names, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": _manifest(names)}})
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"structuredContent": {"hits": [{"path": "app/items.py", "line": 12}]}}})
    return handler


# ---- the layer -------------------------------------------------------------------------


def test_the_eight_graph_tools_are_advertised():
    """D1's third layer. These exist already and a vendor harness ignores them, which is the
    entire opportunity."""
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))

    assert [s.name for s in orientation.specs] == list(ORIENTATION_TOOLS)


def test_the_schemas_come_from_the_server_not_from_a_copy_here():
    """Eight declared duplicates of someone else's contract is a thing that drifts silently."""
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))

    spec = next(s for s in orientation.specs if s.name == "search_code")
    assert spec.description == "search_code does a thing", "the server's words, not ours"
    assert "query" in spec.input_schema["properties"]


def test_a_renamed_tool_refuses_before_a_turn_is_spent():
    """THE FAILURE THIS PREVENTS. The instruction names these tools by name, so advertising
    the other seven leaves the model being told to call something that does not exist —
    discovered one 30-second turn at a time."""
    without = [n for n in ORIENTATION_TOOLS if n != "code_neighbors"]

    with pytest.raises(OrientationUnavailable) as exc:
        orient.build(_server(_listing(without)))

    assert "code_neighbors" in str(exc.value)


def test_an_empty_manifest_says_the_manifest_was_empty_not_that_eight_tools_vanished():
    """Both refuse — the missing-name check would catch this too — so what is under test is
    the MESSAGE, and the first version of this test asserted only the raise and survived
    deleting the guard entirely.

    They are different faults: an empty manifest means the wrong endpoint or a server that
    answered nothing, and reporting it as "no get_code_map, search_code, code_neighbors, …"
    sends the reader looking for eight renames that never happened.
    """
    with pytest.raises(OrientationUnavailable) as exc:
        orient.build(_server(_listing([])))

    assert "empty tool manifest" in str(exc.value)
    assert "code_neighbors" not in str(exc.value), "do not blame a rename for an outage"


def test_a_call_is_forwarded_to_the_server_and_its_answer_returned():
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))

    result = orientation.execute(ToolCall(id="c", name="search_code", input={"query": "claim"}))

    assert result.is_error is False
    assert "app/items.py" in result.content


def test_the_manifest_is_fetched_once_not_per_call():
    """At 22-45s a turn the model's time is what matters, but a fetch per call would also mean
    the tool list could change mid-run under a model that had already read it."""
    calls: list = []
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS, calls)))

    orientation.execute(ToolCall(id="c", name="search_code", input={"query": "x"}))
    orientation.execute(ToolCall(id="c", name="related_work", input={"query": "x"}))

    assert [b["method"] for b in calls] == ["tools/list", "tools/call", "tools/call"]


def test_a_server_refusal_reaches_the_model_as_something_it_can_correct():
    """Same rule as the execution layer: a wrong argument costs a turn, not the run."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": _manifest(ORIENTATION_TOOLS)}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "isError": True,
            "structuredContent": {"error": {"code": "bad_request", "message": "no such node"}}}})

    orientation = orient.build(_server(handler))

    result = orientation.execute(ToolCall(id="c", name="code_neighbors", input={"query": "x"}))

    assert result.is_error is True
    assert "no such node" in result.content


def test_an_outage_mid_run_is_a_result_not_a_crash():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls["n"] += 1
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": _manifest(ORIENTATION_TOOLS)}})
        raise httpx.ConnectError("gone")

    orientation = orient.build(_server(handler))

    result = orientation.execute(ToolCall(id="c", name="get_context", input={}))

    assert result.is_error is True


def test_an_enormous_answer_is_bounded():
    """A `get_code_map` can run to thousands of lines. Bounded here rather than by compaction,
    which only helps after the window has already been filled."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": _manifest(ORIENTATION_TOOLS)}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "structuredContent": {"nodes": ["x" * 200] * 500}}})

    orientation = orient.build(_server(handler))

    result = orientation.execute(ToolCall(id="c", name="get_code_map", input={}))

    assert len(result.content) < orient.MAX_RESULT_CHARS + 200
    assert "truncated" in result.content


def test_every_orientation_tool_is_a_read():
    """What makes handing this layer to a weak model need no further argument. A write here
    would belong in `coord.WORKER_TOOLS` with the give-up path, not among the reads."""
    for name in ORIENTATION_TOOLS:
        assert not name.startswith(("create_", "update_", "claim_", "sign_", "release_",
                                    "assign_", "mint_", "close_", "delete_", "bounce"))


def test_describe_code_is_excluded_even_though_D1_lists_it():
    """A DELIBERATE DEVIATION, pinned so it cannot be undone by tidying.

    Found by running `orient.build` against the live server rather than by reading the PRD:
    `describe_code` is the only one of D1's eight that WRITES. Its own description is "Upsert
    the codebase's structure as a queryable graph of nodes and edges", with a `prune=true`
    that marks unseen nodes stale — so a weak model mid-build can damage the map every other
    agent orients against.

    It is also 1628 characters of schema against 242-535 for the rest: about 40% of the
    orientation budget, on every turn, for a capability a builder has no use for.

    The prefix guard above does not catch it, which is why this is a separate assertion by
    name rather than a cleverer rule.
    """
    assert "describe_code" not in ORIENTATION_TOOLS
    assert "describe_code" in orient.NOT_ORIENTATION
    assert len(ORIENTATION_TOOLS) == 7, "D1 says eight; one of them writes"


# ---- the toolset offers them first ------------------------------------------------------


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "backend").mkdir(parents=True)
    (root / "README.md").write_text("# repo\n")
    return root


def _toolset(root: Path, orientation=None) -> Toolset:
    runner = root / "backend" / "r.sh"
    runner.write_text("#!/bin/sh\necho '1 passed in 1.0s'\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
    return Toolset(root=root, cfg=VerifyConfig(argv=[str(runner)], cwd=root / "backend",
                                               source="r.sh"), orientation=orientation)


def test_the_graph_tools_are_listed_before_the_filesystem_ones(wt):
    """The order a model reads a tool list in is the cheapest nudge available, and D1's whole
    point is that it reaches for the graph first."""
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))

    names = [s.name for s in _toolset(wt, orientation).specs]

    assert names[:len(ORIENTATION_TOOLS)] == list(ORIENTATION_TOOLS)
    assert names.index("search_code") < names.index("grep")
    assert names.index("code_neighbors") < names.index("read_file")


def test_an_agent_with_no_server_still_has_its_execution_tools(wt):
    """Orientation is an addition, not a dependency. The boundary and the test loop are what
    this agent is, and they work with no graph at all."""
    names = [s.name for s in _toolset(wt).specs]

    assert "read_file" in names and "run_tests" in names
    assert "search_code" not in names


def test_a_graph_call_is_routed_to_the_graph_and_a_file_call_is_not(wt):
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))
    toolset = _toolset(wt, orientation)

    graph = toolset.execute(ToolCall(id="1", name="search_code", input={"query": "x"}))
    local = toolset.execute(ToolCall(id="2", name="read_file", input={"path": "README.md"}))

    assert "app/items.py" in graph.content
    assert "# repo" in local.content
    assert orientation.calls == 1, "the filesystem read did not go to the server"


def test_an_unknown_name_lists_the_graph_tools_too(wt):
    """The refusal has to name the surface that exists, and after S6 that surface is bigger."""
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))

    result = _toolset(wt, orientation).execute(ToolCall(id="c", name="nope", input={}))

    assert "search_code" in result.content and "run_tests" in result.content


# ---- the instruction ---------------------------------------------------------------------


def test_the_instruction_names_the_tools_rather_than_saying_orient_first():
    """"Orient first" is advice nobody can follow. A model that does not know
    `code_neighbors` exists will grep."""
    for named in ("search_code", "code_neighbors", "get_item_details", "search_memory"):
        assert named in orient.INSTRUCTION


def test_the_instruction_says_what_to_use_INSTEAD_of_what():
    """A preference with no alternative named is a preference the model cannot act on."""
    assert "before `grep`" in orient.INSTRUCTION
    assert "before reading a file" in orient.INSTRUCTION


def test_the_system_prompt_actually_carries_it():
    """The instruction existing in a module nobody wires in is the failure this catches."""
    assert orient.INSTRUCTION in SYSTEM
    assert "worktree root" in SYSTEM, "the boundary is still stated too"


# ---- the metric (PRD-24 §9.3) -------------------------------------------------------------


class Session:
    def __init__(self, turns):
        self._turns = list(turns)
        self.messages: list[dict] = []
        self.calls = 0

    def run_turn(self, specs):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn

    def add_results(self, results):
        pass


class Coord:
    def write_handoff(self, note): return {}
    def release(self): return {}


def _wants(name, **kw) -> ToolTurn:
    return ToolTurn(tool_calls=[ToolCall(id="c", name=name, input=kw)], wants_tools=True)


def test_the_metric_is_the_turn_the_work_started_on(wt):
    """Turns to first write, because latency is the constraint and the turn is the unit."""
    session = Session([
        _wants("read_file", path="README.md"),
        _wants("read_file", path="README.md"),
        _wants("write_file", path="a.py", content="x = 1\n"),
        # A SECOND write, later. Without it this test cannot tell "first" from "last", and a
        # guard that reassigned on every write would pass it.
        _wants("write_file", path="b.py", content="y = 2\n"),
        ToolTurn(text="DONE", wants_tools=False),
    ])

    outcome = loop.run(session, _toolset(wt), coordinator=Coord(), window=100_000, budget=9)

    assert outcome.turns_to_first_write == 3, "the FIRST write, not the most recent one"
    assert outcome.turns == 5, "total turns is reported alongside, not instead"


def test_a_run_that_never_wrote_reports_None_not_zero(wt):
    """THE LIE THIS AVOIDS. Averaging a never-wrote run in as `0` makes the worst possible
    run — forty turns of reading that changed nothing — look like the best one."""
    session = Session([_wants("read_file", path="README.md")])

    outcome = loop.run(session, _toolset(wt), coordinator=Coord(), window=100_000, budget=4)

    assert outcome.turns_to_first_write is None
    assert outcome.turns_to_first_write != 0


def test_the_metric_survives_a_give_up_so_a_stuck_run_can_still_be_read(wt):
    session = Session([
        _wants("write_file", path="a.py", content="x = 1\n"),
        _wants("read_file", path="README.md"),
    ])

    outcome = loop.run(session, _toolset(wt), coordinator=Coord(), window=100_000, budget=3)

    assert outcome.status == "stuck"
    assert outcome.turns_to_first_write == 1


def test_the_metric_is_not_a_count_of_graph_calls(wt):
    """PRD-24 §9.3, settled. Counting graph calls measures the MEANS: it would score a run
    that called `code_neighbors` three times above one that grepped once and got on with it,
    and it embeds its own conclusion — the tools we added cannot fail if the metric is how
    often they were used."""
    orientation = orient.build(_server(_listing(ORIENTATION_TOOLS)))
    grepper = Session([_wants("grep", pattern="claim"),
                       _wants("write_file", path="a.py", content="x = 1\n"),
                       ToolTurn(text="DONE", wants_tools=False)])
    asker = Session([_wants("search_code", query="claim"), _wants("code_neighbors", query="c"),
                     _wants("get_context"),
                     _wants("write_file", path="a.py", content="x = 1\n"),
                     ToolTurn(text="DONE", wants_tools=False)])

    fast = loop.run(grepper, _toolset(wt, orientation), coordinator=Coord(),
                    window=100_000, budget=9)
    slow = loop.run(asker, _toolset(wt, orientation), coordinator=Coord(),
                    window=100_000, budget=9)

    assert fast.turns_to_first_write < slow.turns_to_first_write, (
        "the run that started work sooner scores better, whichever tools it used"
    )


def test_the_metric_is_written_down_where_it_can_be_argued_with():
    """A metric that lives only in a docstring is one nobody can disagree with before the
    number is claimed."""
    doc = (Path(__file__).resolve().parents[2] / "docs" / "prd-24-orientation-metric.md")

    # Whitespace-normalised: prose gets rewrapped, and a guard that fails when a sentence
    # moves across a line break is noise that teaches people to weaken it.
    text = " ".join(doc.read_text(encoding="utf-8").split())
    assert "turns_to_first_write" in text
    assert "not the arc's success metric" in text.lower()
    assert "allowed to fail" in text, "the walk must be allowed to disprove this"
    assert "obvious proxy is rejected" in text.lower(), "say what was NOT chosen, and why"
