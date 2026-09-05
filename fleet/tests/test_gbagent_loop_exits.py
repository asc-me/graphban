"""Loops have exits (GRPH-709, GRPH-710).

The oracle is deterministic: a fourth `run_tests` on an unchanged tree buys the same answer
for another 22-45 seconds. And an attempt that produced no new information must not be
repeated unchanged. Both are exits the LOOP owns, so the exit is real — the hook-based
version this was ported from could only nag after the fact.
"""
from __future__ import annotations

from pathlib import Path

from gbagent import loop
from gbagent.llm import ToolCall, ToolTurn
from gbagent.toolset import TEST_RUN_CAP, Toolset

from tests.test_gbagent_loop import FakeCoordinator, FakeSession, WINDOW, _toolset, wt  # noqa: F401


def _turn(*calls: tuple[str, dict]) -> ToolTurn:
    return ToolTurn(tool_calls=[ToolCall(id=f"c{i}", name=n, input=kw) for i, (n, kw) in enumerate(calls)],
                    wants_tools=True)


def _run_tests() -> ToolTurn:
    return _turn(("run_tests", {}))


def _edit(path="a.py", content="x = 1\n") -> ToolTurn:
    return _turn(("write_file", {"path": path, "content": content}))


# ---- GRPH-709: the run_tests cap ---------------------------------------------------------------

def test_the_fourth_run_on_an_unchanged_tree_is_refused_and_names_the_last_result(wt):
    ts = _toolset(wt)
    for _ in range(TEST_RUN_CAP):
        assert ts.execute(ToolCall(id="c", name="run_tests", input={})).is_error is False
    fourth = ts.execute(ToolCall(id="c", name="run_tests", input={}))
    assert fourth.is_error is True and "refused" in fourth.content and "PASS" in fourth.content
    assert ts.tests_capped is True and ts.refusals == 1


def test_an_edit_resets_the_cap(wt):
    """Sabotage: drop the reset in `_record` — this fails on the fourth run."""
    ts = _toolset(wt)
    for _ in range(TEST_RUN_CAP):
        ts.execute(ToolCall(id="c", name="run_tests", input={}))
    ts.execute(ToolCall(id="w", name="write_file", input={"path": "a.py", "content": "x = 2\n"}))
    assert ts.execute(ToolCall(id="c", name="run_tests", input={})).is_error is False
    assert ts.tests_capped is False


def test_the_loop_hands_over_when_the_cap_fires_and_the_note_carries_the_last_log(wt):
    """The exit is the loop's: evidence first, release second, exit 75 (D6 order)."""
    coordinator = FakeCoordinator()
    outcome = loop.run(FakeSession([_run_tests()]), _toolset(wt), coordinator=coordinator,
                       window=WINDOW, budget=40)
    assert outcome.status == "stuck" and outcome.exit_code == 75
    assert outcome.turns == TEST_RUN_CAP + 1, "three runs, then the refused fourth ends it"
    assert coordinator.order == ["write_handoff", "release"]
    assert f"ran {TEST_RUN_CAP} times on this change" in coordinator.note
    assert "Tests: passing as of the last run" in coordinator.note


def test_a_failing_suite_rerun_unchanged_ends_on_the_repeat_exit_before_the_cap(wt):
    """Three identical FAIL results are the GRPH-710 repeat, which fires at three — one turn
    before the GRPH-709 cap would. Either way the note carries the failing test's name."""
    coordinator = FakeCoordinator()
    ts = _toolset(wt, prints=("FAILED tests/test_x.py::test_a", "1 failed, 2 passed in 1.0s"), exit_code=1)
    outcome = loop.run(FakeSession([_run_tests()]), ts, coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.exit_code == 75 and outcome.turns == loop.IDENTICAL_FAILURES
    assert "came back 3 times in a row" in coordinator.note
    assert "FAILING" in coordinator.note and "test_a" in coordinator.note


# ---- GRPH-710: repeating failures ---------------------------------------------------------------

def test_three_identical_failing_results_end_the_run(wt):
    """The same refusal three times — here a path outside the worktree, whose message carries
    no varying numbers — is a model repeating itself."""
    coordinator = FakeCoordinator()
    session = FakeSession([_turn(("read_file", {"path": "../../etc/passwd"}))])
    outcome = loop.run(session, _toolset(wt), coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.exit_code == 75 and outcome.turns == loop.IDENTICAL_FAILURES
    assert "came back 3 times in a row" in coordinator.note


def test_identical_means_identical_after_the_numbers_are_dropped(wt):
    assert loop._normalise("FAIL exit 1 (r.py) in 12.3s at line 40") == loop._normalise("FAIL exit 1 (r.py) in 9.7s at line 41")
    assert loop._normalise("no tool named 'x'") != loop._normalise("no tool named 'y'")


def test_three_different_failures_do_not_end_the_run(wt):
    """The child is still learning: each failure is new information."""
    coordinator = FakeCoordinator()
    session = FakeSession([
        _turn(("read_file", {"path": "../../a"})),
        _turn(("read_file", {"file": "b"})),          # bad arguments, a different shape
        _turn(("no_such_tool", {})),                   # unknown tool, another
        ToolTurn(text="done", wants_tools=False, usage={"input": 1, "output": 1}),
    ])
    outcome = loop.run(session, _toolset(wt), coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.status == "finished" and coordinator.order == []


def test_a_success_resets_the_identical_failure_count(wt):
    coordinator = FakeCoordinator()
    bad = _turn(("read_file", {"path": "../../a"}))
    good = _turn(("read_file", {"path": "README.md"}))
    session = FakeSession([bad, bad, good, bad, bad,
                           ToolTurn(text="done", wants_tools=False, usage={"input": 1, "output": 1})])
    outcome = loop.run(session, _toolset(wt), coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.status == "finished", "two, a success, two more: never three in a row"


def test_two_edit_and_test_cycles_on_the_same_failing_set_end_the_run(wt):
    """Sabotage: drop the number normalisation — the two FAIL results differ by their
    durations, the identical-failure counter never fires, and this still fails on the cycle
    counter, which reads the parsed failing set."""
    coordinator = FakeCoordinator()
    ts = _toolset(wt, prints=("FAILED tests/test_x.py::test_a", "FAILED tests/test_x.py::test_b",
                              "2 failed, 1 passed in 1.0s"), exit_code=1)
    session = FakeSession([_edit(content="x = 1\n"), _run_tests(), _edit(content="x = 2\n"), _run_tests(),
                           ToolTurn(text="done", wants_tools=False, usage={"input": 1, "output": 1})])
    outcome = loop.run(session, ts, coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.exit_code == 75 and outcome.turns == 4
    assert "2 edit-and-test cycles ended on the same failing tests" in coordinator.note
    assert "test_a" in coordinator.note and "test_b" in coordinator.note


def test_a_cycle_whose_failing_set_changed_is_progress(wt, tmp_path):
    """Different failing tests after an edit means the change moved something."""
    coordinator = FakeCoordinator()
    ts = _toolset(wt, prints=("FAILED tests/test_x.py::test_a", "1 failed in 1.0s"), exit_code=1)
    # Two cycles: first fails on test_a, then the stub is re-pointed so the second fails on test_b.
    session = FakeSession([_edit(content="x = 1\n"), _run_tests(), _edit(content="x = 2\n"), _run_tests(),
                           ToolTurn(text="done", wants_tools=False, usage={"input": 1, "output": 1})])
    calls = {"n": 0}
    real = ts._do_run_tests

    def flip():
        calls["n"] += 1
        if calls["n"] == 2:
            ts.last_tests = {"ok": False, "command": "r.py", "failed_tests": ["tests/test_x.py::test_b"],
                             "tail": "", "exit_code": 1, "passed": 0, "failed": 1}
            ts.runs_since_edit += 1
            return "FAIL exit 1 (r.py): 1 failed\nfailed:\ntests/test_x.py::test_b\n--- last lines ---\n"
        return real()
    ts._do_run_tests = flip  # type: ignore[assignment]
    outcome = loop.run(session, ts, coordinator=coordinator, window=WINDOW, budget=40)
    assert outcome.status == "finished", "a moving failing set is not a repeat"
