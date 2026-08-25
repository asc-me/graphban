"""The agent loop, its turn budget, and how it gives up (PRD-24 S3, D6, AC-7).

Advertise → call → execute → feed back, until the model stops asking for tools or the budget
runs out. The driver is provider-neutral: it sees `ToolTurn`s, never messages, so the whole
give-up path is testable without a model.

**The measured constraint is latency.** One tool-calling turn against `ms-s1-ubt` is 22.2s
(`qwen3-coder:30b`), 29.7s (`gpt-oss:20b`), 44.8s (`qwen3:30b-a3b`). A budget of 40 turns is
fifteen to thirty minutes, and every choice here that trades turns for tokens takes the trade.

**The order in `_give_up` is correctness, not politeness.** `release_item` clears `built_by`
when `updated_at <= claimed_at` (GRPH-434), and writing to the worktree does not touch the item
row. An agent that wrote two hundred lines and released would have its authorship cleared,
leaving a salvage branch nobody is recorded as having made. So: write, then release, then exit.
If the write fails we do NOT release — a claimed item with a diff is recoverable when the lease
expires, and a released one with cleared authorship is not.

**Compaction is the loop's job, not the model's** (D7, S4). There is no `compact` tool, so
the agent cannot forget to call it or spend a 30-second turn deciding to. The check runs after
every turn on the token count the endpoint itself reported, and acts before the next one.

**The handoff note is assembled from what actually ran**, not from what the model said about it.
`Toolset` watched the dispatch: which files were written, what the last `run_tests` returned,
how many calls were refused. A model that reports "all tests pass" in prose having never called
`run_tests` is the failure this avoids, and it is the same defect class as absence reading as
clean.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import compact as compaction
from .coord import Coordinator, HandoffFailed
from .heartbeat import Heartbeat
from .llm import ToolTurn
from .toolset import Toolset

#: Turns, not tool calls: one turn is one round trip to the model, which is the thing that costs
#: 22-45 seconds. A turn may carry several tool calls and they are all executed.
DEFAULT_BUDGET = 40

#: `0` finished — the normal end of a worker's life (PRD-22 D-c).
EXIT_OK = 0
#: `75` stuck: budget spent, evidence written, item released, worktree left for salvage.
#: Not an arbitrary number — 75 is `EX_TEMPFAIL` in sysexits.h, "temporary failure; the user is
#: invited to retry", which is exactly what a give-up is.
EXIT_STUCK = 75
#: `70` the handoff could not be written, so the item was NOT released and is still claimed.
#: `EX_SOFTWARE`. Deliberately not 75: 75 tells the supervisor the item is back in the queue,
#: and a code that said so falsely would be worse than no code at all.
EXIT_HANDOFF_FAILED = 70


def exit_meaning(code: int) -> str:
    """What this agent's exit code means, in words (AC-7).

    The supervisor tells surrender from failure by reading this rather than by parsing stderr.
    Exiting 0 on a give-up was rejected in D6: `ps` and the supervisor's record would show a
    clean finish for a run that achieved nothing.
    """
    if code == EXIT_OK:
        return "finished"
    if code == EXIT_STUCK:
        return "stuck: turn budget spent, evidence written, item released, worktree salvaged"
    if code == EXIT_HANDOFF_FAILED:
        return (
            "could not write the handoff note; the item was NOT released and is still claimed "
            "until its lease expires"
        )
    return f"crashed (exit {code})"


@dataclass
class Outcome:
    """What happened, and what the process should exit with."""

    status: str  # "finished" | "stuck" | "handoff_failed"
    exit_code: int
    turns: int
    #: The model's closing message on a finish; the failure on a handoff that could not be written.
    text: str = ""
    #: The note that was written to the item, when one was.
    handoff: str = ""
    #: How many times the context was compacted (D7). Zero on a run that never came close.
    compactions: int = 0
    #: The turn on which this run first CHANGED something. `None` means it never did, which
    #: is not turn zero — see `docs/orientation-metric-prd24.md`. This is the orientation
    #: number: how many 22-45 second turns went by before the work started.
    turns_to_first_write: int | None = None
    usage: dict = field(default_factory=dict)

    @property
    def meaning(self) -> str:
        return exit_meaning(self.exit_code)


def run(
    session,
    toolset: Toolset,
    *,
    coordinator: Coordinator,
    window: int,
    budget: int = DEFAULT_BUDGET,
    threshold: float = compaction.DEFAULT_THRESHOLD,
    heartbeat: "Heartbeat | None" = None,
) -> Outcome:
    """Drive the loop until the model stops asking for tools, or the budget is spent.

    `session` is anything with `run_turn(specs) -> ToolTurn` and `add_results(results)` —
    `llm.OllamaSession` in production, a scripted stand-in in the tests. The driver never
    touches a message except to compact it, which is what keeps this provider-neutral and the
    give-up path provable without a network.

    **`window` has no default on purpose.** It is the model's context size in tokens, and the
    two failures of guessing it are not symmetric: assume too large and the run dies of an
    overflow compaction could have prevented; assume too small and it compacts constantly and
    throws away the 262k that made a local model worth using. A default would pick one of those
    silently, so the caller states it — the same argument D3 makes about the test command.
    """
    if budget < 1:
        raise ValueError(f"budget must be at least one turn, got {budget}")
    if window < 1:
        raise ValueError(f"window must be a positive number of tokens, got {window}")

    specs = toolset.specs
    turn: ToolTurn = session.run_turn(specs)
    turns = 1
    compactions = 0

    first_write: int | None = None

    while turn.wants_tools and turn.tool_calls:
        if turns >= budget:
            return _give_up(coordinator, toolset, turns, budget, turn, compactions, first_write)
        if heartbeat is not None and heartbeat.gone:
            # The heartbeat thread heard the server say this agent's claim is gone
            # (GRPH-496). Checked at the TURN BOUNDARY rather than acted on from the thread:
            # interrupting a blocked subprocess from a daemon thread is a much larger
            # decision, and the supervisor already stops disowned children. What this buys
            # is that the child stops before spending ANOTHER 22-45 second turn, and leaves
            # a note saying why instead of dying without one.
            return _give_up(coordinator, toolset, turns, budget, turn, compactions,
                            first_write, why=heartbeat.gone)
        session.add_results([toolset.execute(call) for call in turn.tool_calls])
        if first_write is None and toolset.written:
            # The turn the work started on. Recorded here rather than counted afterwards
            # because the toolset only knows THAT a write happened, not when.
            first_write = turns
        compactions += _maybe_compact(session, turn, window, threshold)
        turn = session.run_turn(specs)
        turns += 1

    if turn.wants_tools and not turn.tool_calls:
        # **A turn that asked for tools and carried none is not a finish** (GRPH-489).
        # `wants_tools` is `finish_reason == "tool_calls" or bool(calls)`, and that `or` is
        # there because local models stop with `finish_reason: "stop"` while carrying tool
        # calls. This is the same disagreement in the other direction — a truncated or
        # malformed `tool_calls` array on a finish_reason of `tool_calls` — and the loop
        # condition above is False for it, so it used to fall straight through and return
        # exit 0 with an empty closing message and no record.
        #
        # `exit_meaning` already names why that is wrong: "Exiting 0 on a give-up was
        # rejected in D6 — ps and the supervisor's record would show a clean finish for a
        # run that achieved nothing." A turn was spent and nothing came of it, which is
        # what EXIT_STUCK means, so it gives up rather than claiming success.
        return _give_up(coordinator, toolset, turns, budget, turn, compactions, first_write,
                        why="the model asked for tools and carried none — a malformed turn, "
                            "not a finish")

    return Outcome(
        status="finished",
        exit_code=EXIT_OK,
        turns=turns,
        text=turn.text,
        usage=turn.usage or {},
        compactions=compactions,
        turns_to_first_write=first_write,
    )


def _maybe_compact(session, turn: ToolTurn, window: int, threshold: float) -> int:
    """Compact if the last turn crossed the threshold. Returns 1 if it did, 0 otherwise.

    The count comes from the endpoint's own `prompt_tokens`, which is the model's tokenizer
    rather than ours. When it reports nothing, `compact.should_compact` estimates instead of
    reading silence as room to spare.

    A session with no `messages` — every stand-in in the tests that is not exercising this —
    simply never compacts, and says so by returning 0 rather than by raising.
    """
    messages = getattr(session, "messages", None)
    if messages is None:
        return 0
    reported = (turn.usage or {}).get("input")
    if not compaction.should_compact(reported, messages, window, threshold):
        return 0
    result = compaction.compact(messages)
    if not result.summarised:
        # Over the threshold with nothing compactable. This branch used to explain itself as
        # "everything large is the plan or the last five turns, which D7 says survive" — and
        # that described the COMMON case as an edge case: tool-call arguments were
        # unreachable, so an agent that had written a few modules landed here with
        # `protected` at zero and no remedy at all (GRPH-490). Arguments are compacted now,
        # so reaching this really does mean everything large is protected.
        #
        # Saying so beats reporting a compaction that freed nothing, and beats looping on an
        # unchanged conversation.
        return 0
    session.messages = result.messages
    return 1


def _give_up(
    coordinator: Coordinator, toolset: Toolset, turns: int, budget: int, turn: ToolTurn,
    compactions: int = 0, first_write: int | None = None, why: str = "",
) -> Outcome:
    """D6, in the one order that preserves the record: write, release, exit 75.

    `why` names a give-up that is NOT the budget running out — currently only the malformed
    turn of GRPH-489. It reaches the handoff note, because "gave up after 3 of 40 turns"
    with no explanation reads as a crash to whoever picks the item up.
    """
    note = handoff_note(toolset, turns, budget, turn, compactions, why)
    try:
        coordinator.write_handoff(note)
    except HandoffFailed as exc:
        # NOT released. See EXIT_HANDOFF_FAILED — a release now would clear `built_by` and
        # throw away the only record of who made the diff sitting in the worktree.
        return Outcome(
            status="handoff_failed",
            exit_code=EXIT_HANDOFF_FAILED,
            turns=turns,
            text=str(exc),
            handoff=note,
            usage=turn.usage or {},
            compactions=compactions,
            turns_to_first_write=first_write,
        )
    coordinator.release()
    return Outcome(
        status="stuck",
        exit_code=EXIT_STUCK,
        turns=turns,
        text=turn.text,
        handoff=note,
        usage=turn.usage or {},
        compactions=compactions,
        turns_to_first_write=first_write,
    )


def handoff_note(toolset: Toolset, turns: int, budget: int, turn: ToolTurn,
                 compactions: int = 0, why: str = "") -> str:
    """Turns spent, what changed, where the tests stand, what to try next (D6).

    Written for the agent that picks this up next, so every line is something that agent would
    otherwise have to rediscover by reading a diff. The tests line is the one that has to be
    exact: **never run** and **ran and passed** are different states, and reporting the first as
    the second is how a half-finished item gets signed off.
    """
    lines = [
        (f"gbagent gave up after {turns} of {budget} turns (PRD-24 D6)."
         if not why else
         f"gbagent stopped after {turns} of {budget} turns: {why} (PRD-24 D6)."),
        "",
        f"Files changed: {', '.join(toolset.written) if toolset.written else 'none'}",
        f"Tests: {_tests_line(toolset.last_tests)}",
    ]
    if compactions:
        lines.append(
            f"Context was compacted {compactions} time(s) — this run was long enough that old "
            "tool output was summarised away, so its early reasoning is no longer verbatim."
        )
    if toolset.refusals:
        lines.append(
            f"Tool calls refused: {toolset.refusals} — check whether it was fighting the "
            "worktree boundary or an edit anchor."
        )
    closing = (turn.text or "").strip()
    lines += [
        "",
        "What it was doing when the budget ran out:",
        closing if closing else "(the model's last turn was a tool call, with no prose)",
        "",
        "The worktree is left dirty for salvage (D9) — nothing was committed.",
    ]
    return "\n".join(lines)


def _tests_line(last: dict | None) -> str:
    if last is None:
        return "NEVER RUN — this is not the same as passing, and nothing here is verified"
    if last["ok"]:
        return f"passing as of the last run ({last['command']})"
    failed = ", ".join(last["failed_tests"]) or "names not parsed from the output"
    return f"FAILING ({last['command']}) — {failed}"
