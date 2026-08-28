"""S4 — compaction that drops tool output and never instructions (PRD-24 D7, AC-9).

**Every test here FORCES the threshold.** The measured windows are 262k and 131k tokens, so a
test that ran the agent and waited to cross 70% would either take an hour or, far more likely,
never compact at all and pass anyway. A compaction suite that never compacts is the "test that
never enters the state it is about" this repository keeps finding, so the window is set to a
number the fixture can actually exceed.

The claim under test is not "compaction happens". It is **what survives it**: the instruction,
the item, the plan, and the last five turns. A run that finishes with a directory listing intact
and its own task forgotten has compacted successfully and failed completely.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gbagent import compact, loop
from gbagent.config import VerifyConfig
from gbagent.llm import ToolCall, ToolTurn
from gbagent.toolset import Toolset
from conftest import make_stub_script, stub_argv  # noqa: E402

BIG = "x" * 4000

# LONG ON PURPOSE. Every one of these is over MIN_SUMMARISE_CHARS, so a compaction that started
# touching non-tool messages would visibly destroy them. Written short, the "the instruction
# survives" assertions passed even when the role check was removed entirely — they were
# asserting that a 40-character string is under the 240-character threshold.
SYSTEM = "You are gbagent. Never leave the worktree. " + "Follow the plan. " * 40
ITEM = "GRPH-999: make the failing test pass. Do not edit the test file. " + "Context. " * 40
PLAN = "Plan: read the failing test, find the defect, fix it, re-run. " + "Then verify. " * 40


def _assistant(text: str = "", call: str | None = None, path: str = "a.py") -> dict:
    message: dict = {"role": "assistant", "content": text}
    if call:
        message["tool_calls"] = [{"id": f"c_{path}", "type": "function", "function": {
            "name": call, "arguments": f'{{"path":"{path}"}}'}}]
    return message


def _tool(path: str, content: str = BIG) -> dict:
    return {"role": "tool", "tool_call_id": f"c_{path}", "content": content}


def _conversation(pairs: int = 12) -> list[dict]:
    """A long run: instruction, item, a plan turn, then many read/result pairs."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ITEM},
                _assistant(PLAN)]
    for n in range(pairs):
        messages.append(_assistant(call="read_file", path=f"file{n}.py"))
        messages.append(_tool(f"file{n}.py"))
    return messages


# ---- what survives ---------------------------------------------------------------------


def test_the_instruction_the_item_and_the_plan_are_never_touched():
    """AC-9's second half, and the whole point of D7. A model that forgets what it was asked to
    do while remembering a directory listing is worse than one that stops.

    All three strings are longer than the summarise threshold, so this fails if compaction ever
    stops discriminating by role — which is the only thing keeping them.
    """
    assert min(len(SYSTEM), len(ITEM), len(PLAN)) > compact.MIN_SUMMARISE_CHARS, \
        "this test asserts nothing if the fixtures are under the threshold anyway"

    result = compact.compact(_conversation())

    assert result.messages[0]["content"] == SYSTEM
    assert result.messages[1]["content"] == ITEM
    assert result.messages[2]["content"] == PLAN


def test_the_last_five_turns_are_kept_verbatim():
    result = compact.compact(_conversation(pairs=12))

    tail = result.messages[-10:]  # five assistant turns and the five results answering them
    assert all("[compacted]" not in str(m.get("content")) for m in tail), \
        "the recent turns are what the model is actually working from"
    assert tail[-1]["content"] == BIG


def test_the_oldest_tool_results_are_summarised():
    result = compact.compact(_conversation(pairs=12))

    assert result.summarised > 0
    assert result.chars_after < result.chars_before
    summarised = [m for m in result.messages if "[compacted]" in str(m.get("content"))]
    assert len(summarised) == result.summarised


def test_a_summary_names_the_tool_and_its_argument():
    """D7's shape: `read_file src/x.py -> 240 lines`. A summary that only says "output was here"
    tells the model nothing about whether to ask again — and the name lives in the assistant
    message, not in the tool message being rewritten."""
    result = compact.compact(_conversation(pairs=12))

    first = next(m for m in result.messages if "[compacted]" in str(m.get("content")))
    assert "read_file" in first["content"]
    assert "file0.py" in first["content"]
    assert "4000 chars" in first["content"]


def test_a_label_is_parsed_from_the_arguments_not_matched_in_them():
    """A `write_file` whose CONTENT contains the characters `"path":` would otherwise have that
    read as its path. Compaction runs on the path a long run depends on, so a malformed
    argument degrades to the bare tool name rather than raising."""
    messages = _conversation(pairs=12)
    messages[3]["tool_calls"][0]["function"] = {
        "name": "write_file",
        "arguments": '{"path":"real.py","content":"cfg = {\\"path\\": 1}"}',
    }
    messages[5]["tool_calls"][0]["function"] = {"name": "grep", "arguments": "not json at all"}

    result = compact.compact(messages)

    assert "write_file real.py" in result.messages[4]["content"]
    assert "grep" in result.messages[6]["content"]


def test_nothing_is_removed_so_no_tool_call_is_left_unanswered():
    """THE FAILURE MODE THAT DELETION WOULD HAVE. Every `tool` message answers a `tool_call`,
    and an endpoint that receives a call with no answer rejects the whole conversation."""
    before = _conversation(pairs=12)

    result = compact.compact(before)

    assert len(result.messages) == len(before)
    calls = [c["id"] for m in result.messages for c in m.get("tool_calls") or []]
    answers = [m["tool_call_id"] for m in result.messages if m.get("role") == "tool"]
    assert sorted(calls) == sorted(answers)


def test_the_input_is_not_mutated():
    """A compaction that half-applied would leave a conversation that is neither the old one
    nor the new one."""
    before = _conversation(pairs=12)

    compact.compact(before)

    assert all(m.get("content") != "" or True for m in before)
    assert before[4]["content"] == BIG, "the caller's list is untouched"


def test_short_results_are_left_alone():
    """Summarising a 40-character refusal into a 60-character note about a refusal makes the
    context bigger and the run worse."""
    messages = _conversation(pairs=12)
    messages[4] = _tool("file0.py", "ERROR: '/etc/passwd' is outside the worktree")

    result = compact.compact(messages)

    assert result.messages[4]["content"] == "ERROR: '/etc/passwd' is outside the worktree"


def test_assistant_reasoning_is_never_summarised():
    """D7 names tool RESULTS. An assistant turn is the model's own intent, and it is also where
    the tool_calls live."""
    messages = _conversation(pairs=12)
    messages[3] = _assistant("I will start with the test file because " + BIG, call="read_file")

    result = compact.compact(messages)

    assert result.messages[3]["content"] == messages[3]["content"]


def test_compaction_is_idempotent():
    """It runs after every turn once a run is long. A second pass must not re-summarise what is
    already a summary, or the numbers reported become fiction."""
    once = compact.compact(_conversation(pairs=12))

    twice = compact.compact(once.messages)

    assert twice.summarised == 0
    assert twice.chars_after == once.chars_after


def test_a_short_conversation_has_nothing_to_compact():
    """Fewer than five assistant turns means everything is recent. That is the right answer,
    not an edge case."""
    result = compact.compact(_conversation(pairs=2))

    assert result.summarised == 0


def test_prose_turns_survive_wherever_they_are_not_only_at_the_start():
    """The plan is kept because it is prose, not because of where it sits. A rule that protected
    index 2 would keep "the plan" on a run that opened with a tool call and wrote its plan on
    the second turn — protecting a message that is not a plan and losing the one that is."""
    messages = _conversation(pairs=12)
    del messages[2]
    messages.insert(5, _assistant("Revised plan: the defect is in the parser. " + "Detail. " * 40))

    result = compact.compact(messages)

    assert result.messages[5]["content"] == messages[5]["content"]
    assert result.summarised > 0, "the old tool results still compact around it"


# ---- the trigger -----------------------------------------------------------------------


def test_the_threshold_is_measured_in_tokens_at_seventy_percent():
    assert compact.DEFAULT_THRESHOLD == 0.70

    assert compact.should_compact(7000, [], window=10_000) is True
    assert compact.should_compact(6999, [], window=10_000) is False


def test_the_endpoints_own_count_is_preferred_over_the_estimate():
    """It comes from the model's tokenizer and cannot be wrong; ours is arithmetic on lengths."""
    tiny = [{"role": "user", "content": "hi"}]

    assert compact.should_compact(9_000, tiny, window=10_000) is True


def test_an_endpoint_that_reports_no_usage_is_estimated_rather_than_assumed_empty():
    """ABSENCE MUST NOT READ AS CLEAN. `usage.get("prompt_tokens", 0)` hands back 0 for an
    endpoint that never reported, and 0 reads as plenty of room left — so compaction would
    never fire for exactly the providers whose behaviour we know least about."""
    big = _conversation(pairs=12)

    assert compact.should_compact(None, big, window=10_000) is True
    assert compact.should_compact(0, big, window=10_000) is True


def test_the_estimator_is_calibrated_and_errs_towards_compacting_early():
    """MEASURED, not assumed: 3.47-4.03 chars/token against qwen3-coder:30b and gpt-oss:20b on
    this repository's own Python, Markdown and tool results. The low end is chosen because an
    estimate that runs high compacts early and one that runs low compacts too late, and only
    one of those loses a run."""
    assert compact.CHARS_PER_TOKEN <= 3.5

    assert compact.estimate_tokens([{"role": "user", "content": "x" * 3500}]) == 1000


def test_the_estimator_counts_tool_call_arguments_not_only_content():
    """A turn asking to write a 40k file carries all of it in `arguments`, where a
    content-only estimate sees an empty string."""
    call_only = [_assistant(call="write_file")]
    call_only[0]["tool_calls"][0]["function"]["arguments"] = '{"content":"' + "y" * 3500 + '"}'

    assert compact.estimate_tokens(call_only) > 900


def test_a_nonsense_window_is_refused_rather_than_dividing_into_it():
    with pytest.raises(ValueError):
        compact.should_compact(100, [], window=0)


# ---- the loop does it, not the model ------------------------------------------------------


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "backend").mkdir(parents=True)
    (root / "big.txt").write_text(BIG)
    return root


def _toolset(root: Path) -> Toolset:
    runner = make_stub_script(root / "backend" / "r.py", prints=("1 passed in 1.0s",))
    return Toolset(root=root, cfg=VerifyConfig(argv=stub_argv(runner), cwd=root / "backend",
                                               source="r.py"))


class ScriptedSession:
    """A session with real message history, so compaction has something to act on."""

    def __init__(self, turns: list[ToolTurn], reported: int | None = None):
        self._turns = list(turns)
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM},
                                     {"role": "user", "content": ITEM},
                                     {"role": "assistant", "content": PLAN}]
        self.calls = 0
        self._reported = reported

    def run_turn(self, specs):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        self.messages.append(_assistant(turn.text, call="read_file", path=f"f{self.calls}.py"))
        if self._reported is not None:
            turn = ToolTurn(text=turn.text, tool_calls=turn.tool_calls,
                            wants_tools=turn.wants_tools, usage={"input": self._reported})
        return turn

    def add_results(self, results):
        for result in results:
            self.messages.append({"role": "tool", "tool_call_id": f"c_f{self.calls}.py",
                                  "content": result.content})


class FakeCoordinator:
    def __init__(self): self.order, self.note = [], ""
    def adopt(self, item_id): self.adopted = item_id
    def write_handoff(self, note): self.order.append("write_handoff"); self.note = note; return {}
    def release(self): self.order.append("release"); return {}


def _reading() -> ToolTurn:
    return ToolTurn(tool_calls=[ToolCall(id="c", name="read_file", input={"path": "big.txt"})],
                    wants_tools=True)


def test_a_run_that_crosses_the_threshold_still_finishes_with_its_instruction_intact(wt):
    """AC-9, forced. The window is set low enough that a handful of 4000-character reads
    crosses it, because waiting for 70% of 262k would never happen in a test."""
    session = ScriptedSession([_reading()] * 8 + [ToolTurn(text="DONE", wants_tools=False)])

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=6_000, budget=12)

    assert outcome.status == "finished" and outcome.exit_code == 0
    assert outcome.compactions > 0, "this run must actually have compacted"
    assert session.messages[0]["content"] == SYSTEM
    assert session.messages[1]["content"] == ITEM
    assert session.messages[2]["content"] == PLAN


def test_a_run_well_inside_the_window_never_compacts(wt):
    session = ScriptedSession([_reading(), ToolTurn(text="DONE", wants_tools=False)])

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=1_000_000, budget=5)

    assert outcome.compactions == 0


def test_the_model_is_never_offered_a_compact_tool(wt):
    """D7: the loop does this. A tool the agent could call is one it can forget to call, call
    wrongly, or spend a 30-second turn on."""
    names = {spec.name for spec in _toolset(wt).specs}

    assert not {n for n in names if "compact" in n or "summar" in n}


def test_the_loop_uses_the_endpoints_reported_count_when_it_has_one(wt):
    """A run whose messages are small but whose reported prompt is enormous — a long system
    preamble, or a provider counting a template we never see — must still compact."""
    session = ScriptedSession([_reading()] * 8 + [ToolTurn(text="DONE", wants_tools=False)],
                              reported=90_000)

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=100_000, budget=12)

    assert outcome.compactions > 0


def test_a_session_without_message_history_simply_never_compacts(wt):
    """The give-up path's stand-ins have no `messages`, and that must not raise."""
    class NoHistory:
        def __init__(self): self.calls = 0
        def run_turn(self, specs):
            self.calls += 1
            return ToolTurn(text="DONE", wants_tools=False)
        def add_results(self, results): pass

    outcome = loop.run(NoHistory(), _toolset(wt), coordinator=FakeCoordinator(), window=10)

    assert outcome.compactions == 0 and outcome.status == "finished"


def test_over_the_threshold_with_nothing_compactable_is_not_counted_as_a_compaction(wt):
    """Everything large is the plan or the last five turns, which D7 says survive. Reporting a
    compaction that freed nothing would make `compactions` a count of attempts rather than of
    work done, and the handoff note would tell the next agent its reasoning was summarised away
    when it was not."""
    session = ScriptedSession([_reading(), _reading(), ToolTurn(text="DONE", wants_tools=False)],
                              reported=999_999)

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=10_000, budget=6)

    assert outcome.status == "finished"
    assert outcome.compactions == 0, "nothing was summarised, so nothing was compacted"


def test_a_run_that_never_compacted_does_not_mention_it_in_the_note(wt):
    coordinator = FakeCoordinator()

    loop.run(ScriptedSession([_reading()] * 4), _toolset(wt), coordinator=coordinator,
             window=1_000_000, budget=2)

    assert "compacted" not in coordinator.note


def test_the_run_refuses_a_window_nobody_declared(wt):
    """Assume too large and the run dies of an overflow; too small and it compacts constantly
    and throws away the 262k that made a local model worth using. A default picks one silently.

    Omitting the argument is the case that matters, and it must be a TypeError from the
    signature — passing `window=0` only proves the validator, which a default would satisfy
    just as happily. That was this test's first version, and adding a default survived it.
    """
    with pytest.raises(TypeError):
        loop.run(ScriptedSession([ToolTurn(text="x", wants_tools=False)]), _toolset(wt),
                 coordinator=FakeCoordinator())

    with pytest.raises(ValueError):
        loop.run(ScriptedSession([ToolTurn(text="x", wants_tools=False)]), _toolset(wt),
                 coordinator=FakeCoordinator(), window=0)


def test_a_compacted_run_says_so_in_its_handoff_note(wt):
    """The next agent inherits a diff and a note. If the early reasoning was summarised away,
    that is something it should be told rather than left to infer."""
    coordinator = FakeCoordinator()
    session = ScriptedSession([_reading()] * 12)

    loop.run(session, _toolset(wt), coordinator=coordinator, window=6_000, budget=8)

    assert "compacted" in coordinator.note
    assert "no longer verbatim" in coordinator.note


# ---- the agent's own output (GRPH-490) --------------------------------------------------
#
# The biggest thing in a coding agent's context is the code it wrote, and it is not a tool
# result. `write_file` carries the whole file body in the assistant message's
# `tool_calls[].function.arguments`; what comes back is a short {path, created, bytes}.
#
# So compaction summarised the receipt and could not reach the payload. `_text_of` counted
# those arguments — its comment says "arguments carry whole file bodies" — which is the
# whole defect: the measurement saw them and the remedy could not.


def _write_call(i: int, body: str) -> list[dict]:
    """One assistant turn asking to write a file, and the short result that comes back."""
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": f"app/m{i}.py", "content": body})},
        }]},
        {"role": "tool", "tool_call_id": f"c{i}",
         "content": json.dumps({"path": f"app/m{i}.py", "created": True, "bytes": len(body)})},
    ]


def _write_heavy(n: int = 10) -> list[dict]:
    body = "def feature():\n    return 42\n" * 300           # ~9 KB, an ordinary module
    msgs = [
        {"role": "system", "content": "You are gbagent. Do not stop until tests pass."},
        {"role": "user", "content": "GRPH-999: build the thing. " + "x" * 500},
        {"role": "assistant", "content": "Plan: write the modules, then run the tests."},
    ]
    for i in range(n):
        msgs += _write_call(i, body)
    return msgs


def test_a_conversation_of_written_files_is_compactable():
    """THE DEFECT. Ten ordinary modules put the context over the threshold, and compaction
    freed nothing at all: summarised=0, protected=0, 0 of 94,607 chars."""
    msgs = _write_heavy()

    result = compact.compact(msgs)

    assert result.summarised > 0, "the agent's own file bodies were unreachable"
    # Five of the ten writes are inside the last-five-turns boundary and MUST survive, so
    # freeing about half is the right answer rather than a shortfall.
    assert result.summarised == 5, f"summarised {result.summarised}, expected the five outside the boundary"
    assert result.freed_chars > 40_000, f"only freed {result.freed_chars} of {result.chars_before}"


def test_the_written_path_survives_so_the_model_can_read_it_back():
    """What makes shortening an argument SAFE, and it is a stronger guarantee than the one
    a read_file RESULT carries: the content is on disk in the agent's own worktree, put
    there by this very call. The path has to survive for `read_file` to get it back."""
    # n=10, because with fewer than five assistant turns `_keep_boundary` protects
    # everything and this test would pass without compacting anything at all.
    msgs = _write_heavy()

    out = compact.compact(msgs).messages

    args = json.loads(out[3]["tool_calls"][0]["function"]["arguments"])
    assert args["path"] == "app/m0.py", "the path was compacted away with the body"
    assert "[compacted]" in args["content"]
    assert "read the file" in args["content"], "the summary must say how to get it back"


def test_the_call_keeps_its_id_and_name():
    """The assistant/tool pairing is what the endpoint validates. Rewriting an argument must
    leave the structure exactly as the model built it — the same reason results are shortened
    in place rather than deleted."""
    msgs = _write_heavy()

    out = compact.compact(msgs).messages

    call = out[3]["tool_calls"][0]
    assert call["id"] == "c0"
    assert call["function"]["name"] == "write_file"
    assert [m.get("tool_call_id") for m in out if m.get("role") == "tool"] == [f"c{i}" for i in range(10)]


def test_arguments_in_the_last_five_turns_are_left_alone():
    """The boundary applies to both halves. A model still working on the file it just wrote
    must be able to see what it wrote."""
    msgs = _write_heavy()

    out = compact.compact(msgs).messages

    last = json.loads(out[-2]["tool_calls"][0]["function"]["arguments"])
    assert "[compacted]" not in last["content"], "the most recent write was summarised away"
    early = json.loads(out[3]["tool_calls"][0]["function"]["arguments"])
    assert "[compacted]" in early["content"], (
        "nothing was compacted at all, so this proves nothing about the boundary"
    )


def test_a_short_argument_is_not_summarised():
    """Same rule as short results: turning a 40-character argument into a 60-character note
    about an argument makes the context bigger and the run worse."""
    msgs = _write_heavy(n=1) + [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "short", "function": {"name": "read_file",
                                        "arguments": json.dumps({"path": "a.py"})}}]},
        {"role": "tool", "tool_call_id": "short", "content": "x = 1"},
    ]

    out = compact.compact(msgs, keep_last_turns=1).messages

    kept = [m for m in out if (m.get("tool_calls") or [{}])[0].get("id") == "short"][0]
    assert json.loads(kept["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}


def test_an_unparseable_argument_is_carried_rather_than_mangled():
    """A malformed argument is rare and mangling one is worse than carrying it — the same
    call `_call_names` makes when it degrades to the bare tool name."""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "bad", "function": {"name": "write_file", "arguments": "not json " + "y" * 400}}]},
        {"role": "tool", "tool_call_id": "bad", "content": "ok"},
        {"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"},
        {"role": "assistant", "content": "c"}, {"role": "assistant", "content": "d"},
        {"role": "assistant", "content": "e"},
    ]

    out = compact.compact(msgs).messages

    assert out[0]["tool_calls"][0]["function"]["arguments"].startswith("not json ")


def test_the_instruction_and_the_plan_still_survive():
    """Restated against the new code path. Neither is a tool result nor a tool call, so both
    survive by construction — but 'by construction' is a claim, and this is the assertion."""
    msgs = _write_heavy()

    out = compact.compact(msgs).messages

    assert out[0]["content"].startswith("You are gbagent")
    assert out[1]["content"].startswith("GRPH-999")
    assert out[2]["content"] == "Plan: write the modules, then run the tests."


# ---- the clock, not just the window (GRPH-514) ---------------------------------------------
#
# D7 triggers at 70% of the context window. S7's run 3 died after 55 minutes with that window
# 80% EMPTY — one turn simply exceeded the 600s request timeout. PRD-24 §2 says the latency
# number is the design constraint, and D7 protects a different quantity.


def test_a_turn_several_times_the_baseline_is_degradation():
    assert compact.slow_turn(seconds=150.0, fastest=30.0) is True


def test_a_uniformly_slow_model_is_not_a_degrading_one():
    """THE CONTROL, and the reason the trigger is relative. `qwen3:30b-a3b` answers in 44.8s
    when healthy and `qwen3-coder:30b` in 22.2s — both measured. A fixed threshold would fire
    every turn on the slow one and never on the fast one, and compacting every turn spends the
    context it was trying to save."""
    assert compact.slow_turn(seconds=60.0, fastest=50.0) is False


def test_a_fast_run_that_doubles_is_still_fast():
    """Below the floor a ratio means nothing: three times 0.1s is 0.3s, which is noise."""
    assert compact.slow_turn(seconds=0.4, fastest=0.1) is False
    assert compact.slow_turn(seconds=44.0, fastest=1.0) is False, "still under the floor"


def test_the_floor_and_the_ratio_must_BOTH_hold():
    """Either alone is wrong: the floor alone fires on a healthy slow model, the ratio alone
    fires on sub-second noise."""
    assert compact.slow_turn(seconds=compact.SLOW_TURN_FLOOR + 1, fastest=100.0) is False
    assert compact.slow_turn(seconds=compact.SLOW_TURN_FLOOR - 1, fastest=0.1) is False


def test_no_baseline_yet_is_not_degradation():
    """Guards the division. A zero baseline would make every turn infinitely slower."""
    assert compact.slow_turn(seconds=999.0, fastest=0.0) is False


def _clock(monkeypatch, per_turn):
    """A fake monotonic clock: `per_turn` seconds for each turn, in order.

    Two readings per turn — the loop times around `run_turn` — so this yields start/end pairs.
    """
    marks = []
    now = 0.0
    for seconds in per_turn:
        marks += [now, now + seconds]
        now += seconds
    steps = iter(marks)
    monkeypatch.setattr(loop.time, "monotonic", lambda: next(steps, now + 1000.0))


def test_a_run_that_slows_down_compacts_even_with_an_empty_window(wt, monkeypatch):
    """The failure this exists for. Run 3 never came near its window and died anyway.

    SEVEN turns, not three: compaction keeps the last five verbatim, so a short run has
    nothing outside the protected region and `_compact_now` correctly does nothing. The first
    version of this test used three turns and asserted a compaction that could never happen —
    it was asserting against a state the run never entered.
    """
    _clock(monkeypatch, [10.0] * 6 + [200.0, 10.0, 10.0])
    session = ScriptedSession([_reading()] * 7 + [ToolTurn(text="DONE", wants_tools=False)])
    seen: list = []

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=100_000_000, budget=9, trace=seen.append)

    assert outcome.compactions > 0, "the window was empty and it still had to compact"
    reason = next(e for e in seen if e.kind == "compact")
    assert "baseline" in reason.text, "the trace must say WHICH reason fired"


def test_a_run_at_a_steady_pace_does_not_compact_on_time(wt, monkeypatch):
    """The control, and it runs LONG ENOUGH TO COMPACT so its silence means something.

    Same seven turns as the test above — so the protected region is not what is keeping this
    at zero — but every turn takes the same 60s. Slow throughout is not degrading.
    """
    _clock(monkeypatch, [60.0] * 9)
    session = ScriptedSession([_reading()] * 7 + [ToolTurn(text="DONE", wants_tools=False)])

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=100_000_000, budget=9)

    assert outcome.compactions == 0, "60s turns throughout is slow, not degrading"
    assert outcome.slowest_turn == 60.0


def test_the_slowest_turn_is_reported(wt, monkeypatch):
    """PRD-24 §2 calls this the design constraint, so a run should be judgeable on it."""
    _clock(monkeypatch, [5.0, 90.0, 5.0])
    session = ScriptedSession([_reading(), ToolTurn(text="DONE", wants_tools=False)])

    outcome = loop.run(session, _toolset(wt), coordinator=FakeCoordinator(),
                       window=100_000_000, budget=4)

    assert outcome.slowest_turn == 90.0
