"""The classifier in `scripts/token_census.py`, which is the deliverable of GRPH-462.

The totals are only worth as much as the bucketing, and the bucketing is where bias
creeps in. These tests exist because the first version of the classifier put 30.8% of
all tokens in its residual bucket — nearly all of it `cd <repo> && <the real command>` —
and reported source inspection at 35.9%. Fixing the prefix stripping alone moved it to
55.5% and settled the question the other way.

A number that swings twenty points on a regex detail is not a measurement until the
regex has tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Same import route as `test_prd_sync.py` uses for `gen_prd_index`. The
# `spec_from_file_location` version fails here: `@dataclass` resolves its annotations
# through `sys.modules[cls.__module__]`, and a module loaded by spec alone is never
# registered there.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import token_census as census  # noqa: E402


# --- the bug that decided the answer -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cd /repo && grep -n 'def foo' app/thing.py",
        "cd /repo && cd backend && rg pattern",
        "cd /repo\ngrep -n pattern file.py",           # newline, not &&
        "S=/tmp/x && cat notes.md",
        'echo "=== section ===" && sed -n "1,40p" file.py',
        "export PYTHONPATH=. && head -20 app/main.py",
    ],
)
def test_a_navigation_prefix_does_not_hide_the_command(command: str):
    """Every rule anchors on `^`, so anything behind a `cd` fell through to the residual.

    This is not a tidy-up: `cd <repo> && grep` is the single most common shape in these
    transcripts, and misfiling it moved the headline from 35.9% to 55.5%.
    """
    assert census.classify("Bash", command) == "source", census.effective_command(command)


def test_a_bare_navigation_is_still_not_source():
    """The control. If stripping prefixes made everything source, the fix above would
    be manufacturing the conclusion rather than measuring it."""
    assert census.classify("Bash", "cd /repo") != "source"
    assert census.classify("Bash", "cd /repo && pytest -q") == "test"
    assert census.classify("Bash", "cd /repo && git commit -m x") == "git_other"


# --- the distinction the finding turns on --------------------------------------------


def _call(kind: str, fingerprint: str, target: str, size: int = 100) -> census.Call:
    return census.Call(kind=kind, name="Read", command="", target=target,
                       fingerprint=fingerprint, result_tokens=size, truncated=False)


def test_an_exact_repeat_and_a_second_question_are_counted_separately():
    """"90% of file looks were re-looks" and "0.5% were exact repeats" can both be true
    of one session, and they say opposite things.

    Reading a 5,000-line file twice to answer two different questions is how you read a
    large file. Reading it twice with the identical command is the only part a cache
    could recover. Adding them together produces a waste figure that is not waste.
    """
    c = census.Census(session="t", calls=[
        _call("source", "same", "fleet.py"),
        _call("source", "same", "fleet.py"),      # exact repeat
        _call("source", "different", "fleet.py"),  # same file, new question
        _call("source", "elsewhere", "items.py"),
    ])
    out = c.repeats("source")
    assert out["exact_repeats"] == 1
    assert out["same_target_repeats"] == 2, "three looks at fleet.py is two repeats"
    assert out["exact_repeats"] < out["same_target_repeats"]


def test_a_session_with_no_repeats_reports_none():
    """The control: without it `repeats` could be returning the call count."""
    c = census.Census(session="t", calls=[
        _call("source", "a", "one.py"), _call("source", "b", "two.py"),
    ])
    out = c.repeats("source")
    assert out["exact_repeats"] == 0 and out["same_target_repeats"] == 0


# --- the distribution, which is the point --------------------------------------------


def test_it_reports_where_the_mass_sits_not_just_the_mean():
    """A handful of enormous answers and a broad spread of small ones have the same
    mean and need completely different fixes."""
    calls = [_call("source", f"f{i}", f"{i}.py", 10) for i in range(90)]
    calls += [_call("source", f"big{i}", f"big{i}.py", 10_000) for i in range(10)]
    out = census.Census(session="t", calls=calls).per_answer("source")

    assert out["p50"] == 10
    assert out["mean"] > 900, "the mean is dragged up by ten calls"
    assert out["top_decile_share"] > 90, "and that is exactly what it should say"


def test_an_empty_bucket_reports_nothing_rather_than_zero():
    """A bucket with no calls has no distribution. Returning zeros would put a real
    number next to a question nobody asked."""
    assert census.Census(session="t", calls=[]).per_answer("source") == {}


# --- the classifier is auditable -----------------------------------------------------


def test_every_rule_names_a_kind_and_matches_something():
    for kind, tool, cmd in census.TOOL_KINDS:
        assert kind and (tool or cmd), f"{kind} matches nothing"


def test_the_ledger_is_separated_from_other_mcp():
    """It is the thing being evaluated — if reading source dominates, the pitch is that
    the ledger absorbs some of it. Folding it into a general MCP bucket would hide the
    baseline the proposal has to beat."""
    assert census.classify("mcp__agentledger__get_backlog", "") == "ledger"
    assert census.classify("mcp__graphban__search_items", "") == "ledger"
    assert census.classify("mcp__context7__query-docs", "") == "mcp_other"


def test_reading_git_history_is_neither_source_nor_plumbing():
    """`git show` returns code. Calling it source inflates the thesis; calling it git
    plumbing hides real reading. It gets its own bucket so the report can be judged."""
    assert census.classify("Bash", "git show HEAD:app/main.py") == "git_read"
    assert census.classify("Bash", "git status --short") == "git_other"


def test_remote_work_is_not_local_source_inspection():
    """`ssh host 'grep ...'` inspects a DEPLOYED instance, not the codebase. Folding it
    into source would answer the question by definition."""
    assert census.classify("Bash", "ssh ubuntu-srv 'grep X ~/app/.env'") == "remote"
