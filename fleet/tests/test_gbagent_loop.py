"""S3 — the loop, the budget, and the order a give-up happens in (PRD-24 D6, AC-7).

Three things are under test and only one of them is the happy path.

**The wire**, because a parser tuned to one model is a parser that breaks on the next one. The
shapes here were taken from `ms-s1-ubt` — `qwen3-coder:30b` and `gpt-oss:20b` — and the
variations (a missing id, malformed arguments, `finish_reason: stop` carrying tool calls) are
the ones a second model actually produces.

**The dispatch**, because every refusal has to reach the model as a result it can correct. A
tool that raises spends the whole run on one wrong path.

**The give-up**, which is the slice. `release_item` clears `built_by` on an untouched row
(GRPH-434), so writing evidence first is what keeps the authorship — and a test that asserts
both calls happened passes just as happily when they happen backwards. The order is asserted.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from gbagent import loop
from gbagent.config import VerifyConfig
from gbagent.coord import WORKER_TOOLS, Coordinator, HandoffFailed
from gbagent.llm import (
    ModelProtocolError,
    ModelUnreachable,
    OllamaSession,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolTurn,
)
from gbagent.toolset import Toolset
from gbfleet.client import Graphban, NotPermitted

SPEC = ToolSpec(name="read_file", description="read", input_schema={"type": "object"})

#: `qwen3-coder:30b`'s measured context. Stated rather than defaulted, for the reason `run`
#: refuses to guess it — see S4's suite for what the threshold does when it is crossed.
WINDOW = 262_144


# ---- the wire ---------------------------------------------------------------------------


def _session(handler, **kw) -> OllamaSession:
    return OllamaSession(
        "http://model.invalid/v1", "qwen3-coder:30b",
        system="s", task="t", transport=httpx.MockTransport(handler), **kw,
    )


def _reply(message: dict, finish: str = "tool_calls", usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 316, "completion_tokens": 38},
    })


def _call(name: str, arguments: str, id_: str | None = "call_dv6ohr4d") -> dict:
    raw = {"index": 0, "type": "function", "function": {"name": name, "arguments": arguments}}
    if id_ is not None:
        raw["id"] = id_
    return raw


def test_tool_call_arguments_are_decoded_at_the_boundary():
    """The endpoint sends `arguments` as a JSON STRING — verified against ms-s1-ubt. A caller
    that received the string would have to know which provider it was talking to."""
    turn = _session(lambda r: _reply({"role": "assistant", "content": "", "tool_calls": [
        _call("read_file", '{"path":"fleet/src/gbagent/loop.py","start":40}')]})).run_turn([SPEC])

    assert turn.wants_tools is True
    assert turn.tool_calls[0].input == {"path": "fleet/src/gbagent/loop.py", "start": 40}
    assert isinstance(turn.tool_calls[0].input, dict)


def test_malformed_arguments_keep_the_call_so_the_model_is_told():
    """Dropping it would leave the turn looking like the model asked for nothing, and the loop
    would report a clean finish for a turn that failed."""
    turn = _session(lambda r: _reply({"role": "assistant", "tool_calls": [
        _call("read_file", '{"path": unquoted}')]})).run_turn([SPEC])

    assert len(turn.tool_calls) == 1 and turn.tool_calls[0].input == {}


def test_arguments_that_decode_to_something_other_than_an_object_are_refused():
    """A model may emit a JSON ARRAY as its arguments. `json.loads` succeeds, so the decode
    guard is the only thing between that and `handler(**["a", "b"])`."""
    turn = _session(lambda r: _reply({"role": "assistant", "tool_calls": [
        _call("read_file", '["fleet/src/gbagent/loop.py", 40]')]})).run_turn([SPEC])

    assert len(turn.tool_calls) == 1, "the call is kept so the model is told, not dropped"
    assert turn.tool_calls[0].input == {}


def test_a_call_without_an_id_still_correlates():
    """Not every endpoint sends one, and the result we feed back carries whatever we put here."""
    turn = _session(lambda r: _reply({"role": "assistant", "tool_calls": [
        _call("read_file", "{}", id_=None)]})).run_turn([SPEC])

    assert turn.tool_calls[0].id, "a synthesised id is still an id"


def test_tool_calls_count_even_when_the_model_says_it_stopped():
    """THE CROSS-MODEL TRAP. Keying only on `finish_reason == 'tool_calls'` silently drops the
    work a model asked for when it stops with `stop` and carries calls anyway."""
    turn = _session(lambda r: _reply(
        {"role": "assistant", "tool_calls": [_call("read_file", "{}")]}, finish="stop",
    )).run_turn([SPEC])

    assert turn.wants_tools is True


def test_a_turn_with_no_tool_calls_is_a_finish():
    turn = _session(lambda r: _reply({"role": "assistant", "content": "done"},
                                     finish="stop")).run_turn([SPEC])

    assert turn.wants_tools is False and turn.text == "done"


def test_usage_is_carried_because_compaction_is_measured_in_tokens():
    turn = _session(lambda r: _reply({"role": "assistant", "content": "x"}, finish="stop",
                                     usage={"prompt_tokens": 900, "completion_tokens": 12},
                                     )).run_turn([SPEC])

    assert turn.usage == {"input": 900, "output": 12}


def test_the_assistant_turn_is_echoed_verbatim():
    """The tool results fed back next correlate against the ids the endpoint wrote, and
    `gpt-oss` also carries a `reasoning` field that a re-built message would discard."""
    session = _session(lambda r: _reply({"role": "assistant", "reasoning": "thinking",
                                         "tool_calls": [_call("read_file", "{}")]}))
    session.run_turn([SPEC])

    assert session.messages[-1]["reasoning"] == "thinking"
    assert session.messages[-1]["tool_calls"][0]["id"] == "call_dv6ohr4d"


def test_results_are_fed_back_in_the_shape_the_endpoint_expects():
    session = _session(lambda r: _reply({"role": "assistant"}))
    session.add_results([ToolResult(id="call_1", content="hello")])

    assert session.messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "hello"}


def test_a_refusal_is_marked_so_the_model_does_not_read_it_as_output():
    """The OpenAI tool message has no error flag, so it is folded into the text. Without it a
    model treats "outside the worktree" as the contents of a file."""
    session = _session(lambda r: _reply({"role": "assistant"}))
    session.add_results([ToolResult(id="c", content="'/etc/passwd' is outside", is_error=True)])

    assert session.messages[-1]["content"].startswith("ERROR: ")


def test_the_tools_are_advertised_in_the_openai_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply({"role": "assistant"}, finish="stop")

    _session(handler).run_turn([SPEC])

    assert seen["tools"][0]["type"] == "function"
    assert seen["tools"][0]["function"]["name"] == "read_file"


def test_an_unreachable_endpoint_is_distinct_from_a_bad_reply():
    """An unattended run cannot ask, so the two have to be told apart in the record."""
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ModelUnreachable):
        _session(dead).run_turn([SPEC])

    with pytest.raises(ModelProtocolError):
        _session(lambda r: httpx.Response(500, text="boom")).run_turn([SPEC])

    with pytest.raises(ModelProtocolError):
        _session(lambda r: httpx.Response(200, json={"nope": 1})).run_turn([SPEC])


# ---- dispatch: every failure is a result -------------------------------------------------


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "backend").mkdir(parents=True)
    (root / "README.md").write_text("# repo\n")
    return root


def _toolset(root: Path, script: str = "#!/bin/sh\necho '3 passed in 1.0s'\n") -> Toolset:
    runner = root / "backend" / "r.sh"
    runner.write_text(script, encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
    return Toolset(root=root, cfg=VerifyConfig(argv=[str(runner)], cwd=root / "backend",
                                               source="r.sh"))


def test_a_path_outside_the_worktree_comes_back_as_a_correctable_error(wt):
    """AC-1's other half: S1 proves the tool refuses; this proves the refusal REACHES the model
    instead of ending the run."""
    result = _toolset(wt).execute(ToolCall(id="c", name="write_file",
                                           input={"path": "../../etc/x", "content": "x"}))

    assert result.is_error is True
    assert "outside the worktree" in result.content


def test_an_unknown_tool_name_lists_what_there_is(wt):
    result = _toolset(wt).execute(ToolCall(id="c", name="run_shell", input={"cmd": "ls"}))

    assert result.is_error is True
    assert "no tool named 'run_shell'" in result.content
    assert "run_tests" in result.content, "the refusal should point at the real surface"


def test_bad_arguments_name_the_schema_rather_than_crashing(wt):
    result = _toolset(wt).execute(ToolCall(id="c", name="read_file", input={"file": "README.md"}))

    assert result.is_error is True
    assert "bad arguments" in result.content and "path" in result.content


def test_a_defect_INSIDE_a_tool_is_not_reported_as_the_models_mistake(wt):
    """A TypeError from binding and a TypeError from a bug in the tool are the same type.

    Reporting the second as "bad arguments" sends the model round the loop trying different
    arguments until the budget runs out, and hides a real defect behind a plausible refusal.
    An adapter fault should read as one.
    """
    ts = _toolset(wt)
    ts._do_read_file = lambda path, start=1, count=0: None + 1  # type: ignore[assignment]

    with pytest.raises(TypeError):
        ts.execute(ToolCall(id="c", name="read_file", input={"path": "README.md"}))

    assert ts.refusals == 0, "this was not a refusal, and must not be counted as one"


def test_a_successful_call_is_not_marked_an_error(wt):
    result = _toolset(wt).execute(ToolCall(id="c", name="read_file", input={"path": "README.md"}))

    assert result.is_error is False and "# repo" in result.content


def test_the_toolset_records_what_actually_ran(wt):
    """The handoff note is built from this rather than from the model's prose."""
    ts = _toolset(wt)
    ts.execute(ToolCall(id="1", name="write_file", input={"path": "a.py", "content": "x = 1\n"}))
    ts.execute(ToolCall(id="2", name="run_tests", input={}))

    assert ts.written == ["a.py"]
    assert ts.last_tests is not None and ts.last_tests["ok"] is True


def test_unreadable_test_counts_are_reported_as_unreadable_not_as_zero(wt):
    """Carrying S2's honesty through to the string the model reads."""
    ts = _toolset(wt, script="#!/bin/sh\necho 'kaboom'\nexit 3\n")

    content = ts.execute(ToolCall(id="1", name="run_tests", input={})).content

    assert "unreadable" in content and "0 failed" not in content


# ---- the loop -------------------------------------------------------------------------------


class FakeSession:
    """A scripted model. The driver never touches a message, so this is all it needs to be."""

    def __init__(self, turns: list[ToolTurn]):
        self._turns = list(turns)
        self.results: list[list[ToolResult]] = []
        self.calls = 0

    def run_turn(self, specs):
        self.calls += 1
        return self._turns[min(self.calls - 1, len(self._turns) - 1)]

    def add_results(self, results):
        self.results.append(results)


class FakeCoordinator:
    """Records WHAT was called and IN WHICH ORDER, which is the property under test."""

    def __init__(self, fail_handoff: bool = False):
        self.order: list[str] = []
        self.note = ""
        self._fail = fail_handoff

    def adopt(self, item_id):
        self.adopted = item_id

    def write_handoff(self, note: str):
        self.order.append("write_handoff")
        self.note = note
        if self._fail:
            raise HandoffFailed("server said no")
        return {}

    def release(self):
        self.order.append("release")
        return {}


def _wants(name: str = "read_file", **kw) -> ToolTurn:
    return ToolTurn(tool_calls=[ToolCall(id="c", name=name, input=kw)], wants_tools=True)


def _done(text: str = "all done") -> ToolTurn:
    return ToolTurn(text=text, wants_tools=False, usage={"input": 10, "output": 2})


def test_a_run_that_finishes_exits_zero(wt):
    outcome = loop.run(FakeSession([_done()]), _toolset(wt), coordinator=FakeCoordinator(), window=WINDOW)

    assert outcome.status == "finished"
    assert outcome.exit_code == loop.EXIT_OK and outcome.turns == 1
    assert outcome.text == "all done"


def test_tools_are_executed_and_fed_back_before_the_next_turn(wt):
    session = FakeSession([_wants(path="README.md"), _done()])

    loop.run(session, _toolset(wt), coordinator=FakeCoordinator(), window=WINDOW)

    assert session.calls == 2
    assert len(session.results) == 1 and "# repo" in session.results[0][0].content


def test_exhausting_the_budget_writes_evidence_BEFORE_it_releases(wt):
    """AC-7, and the whole reason this slice has a fixed order.

    A test asserting that both calls happened passes just as happily when they happen
    backwards — and backwards is the defect: `release_item` clears `built_by` on a row nothing
    has written to (GRPH-434), so a release-then-write loses the authorship for the diff sitting
    in the worktree.
    """
    coordinator = FakeCoordinator()

    outcome = loop.run(FakeSession([_wants(path="README.md")]), _toolset(wt),
                       coordinator=coordinator, window=WINDOW, budget=3)

    assert coordinator.order == ["write_handoff", "release"]
    assert outcome.status == "stuck" and outcome.exit_code == 75
    assert outcome.turns == 3


def test_the_budget_is_counted_in_model_turns(wt):
    session = FakeSession([_wants(path="README.md")])

    loop.run(session, _toolset(wt), coordinator=FakeCoordinator(), window=WINDOW, budget=5)

    assert session.calls == 5, "five round trips is what five turns buys"


def test_a_budget_of_one_buys_exactly_one_turn(wt):
    """The off-by-one that would cost 22-45 seconds every run, or spend a turn that was never
    authorised."""
    session = FakeSession([_wants(path="README.md")])
    coordinator = FakeCoordinator()

    outcome = loop.run(session, _toolset(wt), coordinator=coordinator, window=WINDOW, budget=1)

    assert session.calls == 1
    assert outcome.exit_code == 75 and coordinator.order == ["write_handoff", "release"]


def test_a_budget_below_one_is_refused_rather_than_looping_or_no_opping(wt):
    with pytest.raises(ValueError):
        loop.run(FakeSession([_done()]), _toolset(wt), coordinator=FakeCoordinator(), window=WINDOW, budget=0)


def test_a_failed_handoff_does_NOT_release_the_item(wt):
    """The item stays claimed until its lease expires, which is recoverable. A released item
    with cleared authorship is not, so 75 — which tells the supervisor the item is back in the
    queue — would be a lie here."""
    coordinator = FakeCoordinator(fail_handoff=True)

    outcome = loop.run(FakeSession([_wants(path="README.md")]), _toolset(wt),
                       coordinator=coordinator, window=WINDOW, budget=1)

    assert coordinator.order == ["write_handoff"], "release must not have been reached"
    assert outcome.status == "handoff_failed"
    assert outcome.exit_code == loop.EXIT_HANDOFF_FAILED != loop.EXIT_STUCK


def test_the_handoff_note_says_what_ran_where_the_tests_stand_and_what_is_next(wt):
    ts = _toolset(wt)
    session = FakeSession([
        _wants("write_file", path="a.py", content="x = 1\n"),
        _wants("run_tests"),
        ToolTurn(text="stuck on the import cycle in a.py", wants_tools=True,
                 tool_calls=[ToolCall(id="c", name="read_file", input={"path": "README.md"})]),
    ])
    coordinator = FakeCoordinator()

    loop.run(session, ts, coordinator=coordinator, window=WINDOW, budget=3)

    note = coordinator.note
    assert "3 of 3 turns" in note
    assert "a.py" in note
    assert "passing as of the last run" in note
    assert "stuck on the import cycle" in note
    assert "nothing was committed" in note


def test_a_note_for_a_run_that_never_tested_says_so_rather_than_going_quiet(wt):
    """ABSENCE MUST NOT READ AS CLEAN. `None` is not a pass, and a note that simply omits the
    tests line lets the next agent assume they were fine."""
    coordinator = FakeCoordinator()

    loop.run(FakeSession([_wants(path="README.md")]), _toolset(wt),
             coordinator=coordinator, window=WINDOW, budget=1)

    assert "NEVER RUN" in coordinator.note
    assert "not the same as passing" in coordinator.note


def test_a_note_for_a_failing_run_names_the_failing_tests(wt):
    ts = _toolset(wt, script=(
        "#!/bin/sh\necho 'FAILED tests/test_a.py::test_boundary'\n"
        "echo '1 failed, 2 passed in 1.0s'\nexit 1\n"))
    coordinator = FakeCoordinator()

    loop.run(FakeSession([_wants("run_tests")]), ts, coordinator=coordinator, window=WINDOW, budget=2)

    assert "FAILING" in coordinator.note
    assert "tests/test_a.py::test_boundary" in coordinator.note


def test_refusals_are_counted_in_the_note_because_they_are_a_different_failure(wt):
    coordinator = FakeCoordinator()

    loop.run(FakeSession([_wants(path="../../etc/passwd")]), _toolset(wt),
             coordinator=coordinator, window=WINDOW, budget=2)

    assert "refused: 1" in coordinator.note


# ---- exit codes -----------------------------------------------------------------------------


def test_exit_meaning_tells_surrender_from_a_crash():
    """AC-7. The supervisor reads this rather than parsing stderr."""
    assert loop.exit_meaning(0) == "finished"
    assert "stuck" in loop.exit_meaning(75)
    assert "released" in loop.exit_meaning(75)
    assert "crashed" in loop.exit_meaning(1)
    assert "crashed" in loop.exit_meaning(139)


def test_the_stuck_code_is_not_the_crash_code_or_zero():
    assert loop.EXIT_STUCK == 75, "sysexits EX_TEMPFAIL — retry is invited, which is the case"
    assert len({loop.EXIT_OK, loop.EXIT_STUCK, loop.EXIT_HANDOFF_FAILED}) == 3


def test_a_handoff_failure_does_not_claim_the_item_was_released():
    """The code exists to stop 75 being told about a run that never released anything."""
    meaning = loop.exit_meaning(loop.EXIT_HANDOFF_FAILED)

    assert "NOT released" in meaning and "still claimed" in meaning


# ---- what the agent may say to the server ---------------------------------------------------


def _graphban(handler, allowed=WORKER_TOOLS) -> Graphban:
    return Graphban("http://graphban.invalid", "gbk_seat",
                    allowed=allowed, transport=httpx.MockTransport(handler))


def _mcp(payload: dict, id_: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": id_,
                                     "result": {"structuredContent": payload}})


def test_the_worker_set_is_pinned_and_is_not_the_supervisors():
    """A test that only asserts `sign_off not in WORKER_TOOLS` passes for every widening.

    Pinned as two halves rather than one flat list, because the halves mean different things:
    the WRITES are what the loop itself initiates, and the READS are the orientation layer the
    model calls. A new write appearing among the reads is the change worth noticing.
    """
    from gbagent.orient import COORDINATION_TOOLS, ORIENTATION_TOOLS
    from gbfleet.client import ALLOWED_TOOLS

    reads, writes = set(ORIENTATION_TOOLS), set(COORDINATION_TOOLS)

    assert reads <= WORKER_TOOLS and writes <= WORKER_TOOLS
    assert not reads & writes, "the reads-only claim depends on these staying separate"
    # `release_item` is the only verb NEITHER layer advertises: the give-up path calls it and
    # the model never should, because releasing is what a harness does when it runs out of
    # turns, not a move a model makes. `update_item` is deliberately in both — the loop writes
    # the handoff note with it and the model moves the item to review with it.
    assert WORKER_TOOLS - reads - writes == {"release_item"}
    assert "update_item" in writes and "release_item" not in writes
    assert not WORKER_TOOLS & ALLOWED_TOOLS, "a worker is not a supervisor"


def test_the_agent_still_cannot_claim_or_judge_its_own_work():
    """The set grew by seven reads in S6 and three writes in S7, and this is the assertion
    that says what it did NOT grow by.

    `claim_next` is here now — an agent that cannot take its own work is not a fleet member.
    What is still absent is every verb that JUDGES: done is not the agent's word (D5), and the
    server enforces that on authorship regardless of what this set says.
    """
    for forbidden in ("claim_review", "sign_off", "bounce", "mint_enrolment",
                      "assign_role", "retire_wave", "close_prd"):
        assert forbidden not in WORKER_TOOLS


def test_the_agent_cannot_sign_off_its_own_work_at_the_client_either():
    """D5 is enforced by the server — `independent()` refuses the author whatever role it
    holds. This is the near half: the agent does not even carry the verb."""
    with pytest.raises(NotPermitted) as exc:
        _graphban(lambda r: _mcp({})).call("sign_off", id="GRPH-1")

    assert "sign_off" in str(exc.value)


def test_the_handoff_sends_only_its_own_note(wt):
    """GRPH-494 made the server the one that keeps the record: `append_evidence` never removes,
    and an identical receipt is a retry rather than a second one. This used to read the item and
    re-send everything on it, back when `update_item` assigned over the stored list — carrying
    that forward would be a round trip to re-send rows the server already keeps, and a second
    way for the one call that must not fail to fail."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["params"])
        return _mcp({"id": "GRPH-1"}, id_=body["id"])

    Coordinator(client=_graphban(handler), item_id="GRPH-1").write_handoff("second agent")

    assert [p["name"] for p in sent] == ["update_item"], "one call, no read"
    assert sent[0]["arguments"]["evidence"] == [{"kind": "note", "detail": "second agent"}]


def test_a_handoff_the_server_refuses_does_not_pretend_it_was_written(wt):
    """`HandoffFailed` is what stops `_give_up` reaching the release."""
    with pytest.raises(HandoffFailed):
        Coordinator(client=_graphban(lambda r: httpx.Response(503, text="down")),
                    item_id="GRPH-1").write_handoff("mine")


def test_release_names_the_agent_so_the_server_can_check_the_lease():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["params"])
        return _mcp({"id": "GRPH-1"}, id_=body["id"])

    Coordinator(client=_graphban(handler), item_id="GRPH-1", agent_id="agt_9").release()

    assert sent[0]["arguments"] == {"id": "GRPH-1", "agent_id": "agt_9"}
