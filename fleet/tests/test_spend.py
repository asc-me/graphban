"""A wave says what it spent, and a budget ends it (GRPH-834).

Reported as O7: items resolve to a frontier model whenever the brief says so, and several did;
`until` had no budget flag and its terminal JSON reported no spend. For unattended operation
that is the number an operator most needs and least has.

**In tokens, not currency.** PRD-41 §7 already defines cost for this system as
tokens-to-sign-off. A dollar figure needs a price table per vendor and model, which goes stale
silently — and a stale price is worse than none, because it is a number somebody will act on.

Two claims are pinned here, and the second is the one that makes the first safe:

1. the numbers the vendors already print reach the wave summary — they reached the LEDGER
   already, and the summary was the one place that never saw them;
2. a wave never reports a total it did not measure, and a budget over an adapter that reports
   nothing is refused rather than accepted and quietly never enforced.
"""
from __future__ import annotations

import pytest

from gbfleet import spend
from gbfleet import until
from gbfleet.adapters import ADAPTERS, reports_tokens

ONE = {"gb/w-1": {"adapter": "gbagent", "tokens_in": 100, "tokens_out": 50, "turns_used": 3}}
TWO = {**ONE,
       "gb/w-2": {"adapter": "gbagent", "tokens_in": 900, "tokens_out": 40, "turns_used": 7}}


# ---- 1. the numbers arrive -----------------------------------------------------------------

def test_the_summary_carries_what_the_children_reported():
    got = spend.totals(TWO, spawned=2)

    assert got["tokens_in"] == 1000 and got["tokens_out"] == 90
    assert got["tokens"] == 1090
    assert [c["branch"] for c in got["by_child"]] == ["gb/w-1", "gb/w-2"]


def test_a_wave_that_measured_nothing_still_reports_a_block():
    """An ABSENT key reads as "this build has no spend reporting"; a zeroed block with
    `reported: 0` reads as "nobody told us", which is the true statement and the one an
    operator can act on."""
    got = spend.totals({}, spawned=4)

    assert got["tokens"] == 0
    assert got["reported"] == 0 and got["unreported"] == 4


# ---- 2. never a total it did not measure --------------------------------------------------

def test_the_children_that_said_nothing_are_counted_separately():
    """THE ONE THAT MATTERS. "1090 tokens" reads as the wave's cost. It is not the wave's cost
    if four of six children were never counted, and a summary that could not say so would
    understate every mixed-adapter wave by whatever the silent half spent.

    Sabotage: derive `unreported` from `len(by_child)` instead of from `spawned` — it becomes
    zero always, and the number above starts reading as complete."""
    got = spend.totals(TWO, spawned=6)

    assert got["reported"] == 2
    assert got["unreported"] == 4


def test_the_budget_is_compared_against_measured_tokens_only():
    """Filling in a guess for the children that said nothing would make the budget fire on
    invented numbers — a worse failure than not firing, because the wave ends and the operator
    cannot tell why."""
    assert spend.spent(TWO) == 1090
    assert spend.over(TWO, 2000) == ""
    assert "1090 measured" in spend.over(TWO, 1000)


def test_the_budget_sentence_says_how_many_reported():
    """"Over budget" is a claim the operator will check, and it is only true of the children
    that reported."""
    said = spend.over(ONE, 100)

    assert "1 child(ren) that reported" in said
    assert "Not spawning further" in said


def test_no_budget_never_ends_a_wave():
    """The control. Almost every wave runs without one."""
    assert spend.over(TWO, None) == ""
    assert spend.over(TWO, 0) == ""


# ---- the refusal that makes the flag honest -------------------------------------------------

def test_a_budget_over_an_adapter_that_reports_nothing_is_refused():
    """THE ONE THAT MAKES IT SAFE. A cap over a vendor that prints no result record is not a
    loose cap — it can never be exceeded, so it never fires, and the operator watches a wave
    run to completion believing it was bounded.

    Sabotage: return early from `check_budget_can_be_enforced` and every other test here stays
    green while `--budget 500 --adapter claude` silently means nothing."""
    with pytest.raises(until.ConfigError) as exc:
        until.check_budget_can_be_enforced("claude", 500)

    said = str(exc.value)
    assert "reports no token usage" in said
    assert "gbagent" in said, "refused without naming an adapter that works"


def test_an_adapter_that_reports_is_accepted():
    until.check_budget_can_be_enforced("gbagent", 500)


def test_reporting_is_derived_from_the_reader_not_a_second_declaration():
    """Two sources for one fact is how they drift. Adding a `result_facts` reader is what makes
    a vendor reportable, so a flag saying otherwise could only ever be wrong."""
    assert reports_tokens("gbagent") is True
    assert [n for n in ADAPTERS if reports_tokens(n)] == ["gbagent"]
    assert reports_tokens("no-such-adapter") is False
