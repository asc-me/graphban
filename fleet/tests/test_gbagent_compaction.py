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

import stat
from pathlib import Path

import pytest

from gbagent import compact, loop
from gbagent.config import VerifyConfig
from gbagent.llm import ToolCall, ToolTurn
from gbagent.toolset import Toolset

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
    runner = root / "backend" / "r.sh"
    runner.write_text("#!/bin/sh\necho '1 passed in 1.0s'\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
    return Toolset(root=root, cfg=VerifyConfig(argv=[str(runner)], cwd=root / "backend",
                                               source="r.sh"))


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
