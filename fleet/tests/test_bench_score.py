from __future__ import annotations

import pytest

from gbfleet.bench import score as score_mod
from gbfleet.bench.score import NOT_REPORTED, TaskOutcome, render_reported, score_run


def b(i, outcome, caps=("A1",), **kw):
    return TaskOutcome(f"b{i}", "build", caps, outcome, **kw)


def r(i, outcome, expect, **kw):
    return TaskOutcome(f"r{i}", "review", ("F1", "F2"), outcome, expect=expect, **kw)


def test_every_build_outcome_and_exclusion():
    outs = [b(1, "check_green"), b(2, "check_red"), b(3, "no_branch"), b(4, "timed_out"),
            b(5, "never_registered"), b(6, "harness_error")]
    s = score_run(outs)
    assert (s.n, s.excluded, s.green) == (4, 2, 1)
    assert s.score == 0.25
    assert s.by_outcome["harness_error"] == 1


def test_plumbing_only_run_has_no_score():
    s = score_run([b(1, "never_registered"), b(2, "harness_error")])
    assert (s.n, s.excluded, s.score) == (0, 2, None)


def test_uncovered_leaf_is_not_covered_and_not_in_n():
    s = score_run([b(1, "check_green")], suite_leaves={"A1"})
    nc = {c.leaf: c.suite for c in s.not_covered}
    assert nc["A2"] == "repo" and nc["A3"] == "template"
    assert "A1" not in nc
    assert all(c.key != "A2" for c in s.cells)
    assert s.n == 1


def test_below_floor_leaf_rolls_up_to_family():
    outs = [b(i, "check_green", ("A1",)) for i in range(3)] + [
        b(i + 10, "check_red", ("A3",)) for i in range(3)]
    s = score_run(outs, suite_leaves={"A1", "A3"})
    cells = {c.key: c for c in s.cells}
    assert cells["A1"].below_floor and cells["A1"].n == 3
    fam = cells["A"]
    assert fam.family and fam.n == 6 and not fam.below_floor and fam.value == 0.5


def test_full_cell_not_below_floor():
    s = score_run([b(i, "check_green") for i in range(5)], suite_leaves={"A1"})
    assert not {c.key: c for c in s.cells}["A1"].below_floor
    assert all(not c.family for c in s.cells)


def test_review_signoff_on_planted_defect_is_f2_miss():
    s = score_run([r(1, "sign_off", "bounce"), r(2, "bounce", "bounce")])
    assert (s.f2, s.f2_n) == (0.5, 2)
    assert s.f1 == 1.0


def test_review_bounce_on_clean_counts_against_f1():
    s = score_run([r(1, "bounce", "sign_off"), r(2, "bounce", "bounce")])
    assert (s.f1, s.f1_n) == (0.5, 2)
    assert s.f2 == 1.0


def test_review_plumbing_excluded():
    s = score_run([r(1, "harness_error", "bounce"), r(2, "bounce", "bounce")])
    assert s.excluded == 1 and s.f2_n == 1


def test_missing_tokens_not_reported_never_zero():
    s = score_run([b(1, "check_green"), b(2, "check_red")])
    assert s.tokens is None and s.wall_seconds is None
    assert render_reported(s.tokens) == NOT_REPORTED
    assert render_reported(0) == "0"
    s = score_run([b(1, "check_green", tokens=10), b(2, "check_red")])
    assert s.tokens == 10


def test_bad_outcome_refused():
    with pytest.raises(ValueError):
        score_run([b(1, "bogus")])
    with pytest.raises(ValueError):
        score_run([TaskOutcome("x", "review", ("F1",), "bounce")])
    assert score_mod.FLOOR == 5


def test_leaf_verdicts_score_each_leaf_by_its_own_predicate():
    from gbfleet.bench.score import TaskOutcome, score_run

    rec = TaskOutcome("t", "build", ("A1", "E1"), "check_red",
                      leaves={"A1": ("fail", ""), "E1": ("pass", "")})
    cells = {c.key: c for c in score_run([rec]).cells}
    assert cells["A1"].value == 0.0 and cells["E1"].value == 1.0


def test_excluded_leaf_is_out_of_that_leaf_n():
    from gbfleet.bench.score import TaskOutcome, score_run

    rec = TaskOutcome("t", "build", ("H5",), "check_green", leaves={"H5": ("excluded", "none")})
    assert {c.key: c.n for c in score_run([rec]).cells}["H5"] == 0
