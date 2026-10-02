"""A predicate that could not run must not report a pass (GRPH-1007), and an honest test
summary must not read as a denial (GRPH-1008).

Two halves of one habit: a gate answering a question it never asked. The first said
`passed: True` for a comparison nobody made, and the completion gate reads only that flag —
so an item reached `done` carrying "5 predicate(s), all passed" with two unperformed. The
second read the word "skipped" inside a quoted pytest summary as the builder denying a
clause, and told them to delete the number, rewarding the thinner receipt.
"""
import pytest

from app.services.fleet import _predicate, acceptance_contradicted
from app.services.items import (
    _normalize_predicates, attested_predicates, missing_predicates, valid_attestations)


def _att(preds, commit="c0ffee123456"):
    return [{"kind": "attestation", "adapter": "fleet.sign_off", "commit": commit,
             "predicates": preds}]


def _p(name, **kw):
    out = {"name": name, "passed": True, "compared": True, "detail": "d"}
    out.update(kw)
    return out


# ── GRPH-1007: the PRODUCER ────────────────────────────────────────────────────────────
#
# These test `_predicate` itself. The gate tests below build their attestation dicts by hand,
# so they pin what the gate DOES with a flag and say nothing about who sets it — and the
# first sabotage run proved it: reverting `_predicate` to hardcode `passed: True` left all
# eighteen of them green. Sabotage the call, not only the callee.

def test_a_predicate_whose_check_ran_reports_passed():
    q = _predicate("commit_is_not_the_base", compared=True, detail="d", waived={})
    assert q["passed"] is True and q["compared"] is True


def test_a_predicate_whose_check_could_not_run_reports_failed():
    """THE 1007 regression, at its source. This used to be a hardcoded `passed: True` with
    the truth left in the prose."""
    q = _predicate("commit_is_not_the_base", compared=False, detail="no base recorded",
                   waived={})
    assert q["passed"] is False and q["compared"] is False
    assert "waived" not in q


def test_a_waiver_is_recorded_on_an_uncompared_predicate():
    q = _predicate("commit_is_not_the_base", compared=False, detail="d",
                   waived={"commit_is_not_the_base": "built by hand, PR #913"})
    assert q["passed"] is False, "a waiver records acceptance, it does not make a check run"
    assert q["waived"] == "built by hand, PR #913"


def test_a_waiver_is_not_recorded_on_a_predicate_that_ran():
    """Belt and braces with the gate's own refusal: the reason never even reaches the receipt
    for a check that happened, so there is nothing for a later reader to misread."""
    q = _predicate("commit_is_not_the_base", compared=True, detail="d",
                   waived={"commit_is_not_the_base": "let me through"})
    assert "waived" not in q


def test_a_waiver_for_a_different_predicate_does_not_apply():
    q = _predicate("commit_is_not_the_base", compared=False, detail="d",
                   waived={"independent_review": "someone else's reason"})
    assert "waived" not in q and q["passed"] is False


# ── GRPH-1007: the gate ────────────────────────────────────────────────────────────────

def test_an_uncompared_predicate_does_not_let_an_item_through():
    """THE regression. Before this, `commit_is_not_the_base` with no recorded base reported
    `passed: True` and the gate admitted it."""
    att = _att([_p("suite_green"),
                _p("commit_is_not_the_base", passed=False, compared=False)])

    assert valid_attestations(att) == []


def test_a_waived_uncompared_predicate_is_admitted():
    """A hand-built item legitimately has no recorded base. The waiver is how a reviewer
    accepts that, on the record — it does not make the predicate pass."""
    att = _att([_p("suite_green"),
                _p("commit_is_not_the_base", passed=False, compared=False,
                   waived="built by hand, PR #913")])

    assert len(valid_attestations(att)) == 1


def test_a_waiver_cannot_launder_a_predicate_that_ran_and_failed():
    """The half that stops the override becoming a bypass. `compared: True, passed: False` is
    a real refusal — the commit WAS the base, the reviewer WAS the author — and no reason
    string reaches it."""
    att = _att([_p("commit_is_not_the_base", passed=False, compared=True,
                   waived="please let me through")])

    assert valid_attestations(att) == []


def test_an_empty_waiver_admits_nothing():
    """A reason that is blank or whitespace is not a record of anything."""
    for blank in ("", "   ", None):
        att = _att([_p("commit_is_not_the_base", passed=False, compared=False, waived=blank)])
        assert valid_attestations(att) == [], blank


def test_an_ordinary_passing_attestation_still_passes():
    """The control. A change that admitted nothing would satisfy three of the four above."""
    assert len(valid_attestations(_att([_p("suite_green")]))) == 1


def test_a_predicate_without_the_compared_field_is_read_as_before():
    """Stored rows predate this field. An older receipt whose predicates all passed must keep
    working — the field's absence is not a claim that the check did not run."""
    att = _att([{"name": "suite_green", "passed": True, "detail": "CI"}])

    assert len(valid_attestations(att)) == 1


def test_a_string_false_still_does_not_pass():
    """GRPH-542's hole must survive the new arm: a JSON client sending "false" is not a pass,
    and it does not have `compared: False` either, so it cannot slip through the waiver path."""
    att = _att([{"name": "suite_green", "passed": "false", "detail": "CI"}])

    assert valid_attestations(att) == []


# ── GRPH-1008: an honest summary is not a denial ───────────────────────────────────────

CLAUSE = "both DB engines green"


def _ev(detail):
    return [{"kind": "test", "detail": f"{CLAUSE} — {detail}"}]


@pytest.mark.parametrize("summary", [
    "SQLite 4656 passed, 34 skipped, 0 red",
    "1458 passed, 7 skipped, 1 warning in 343.14s",
    "4686 passed, 4 skipped",
    "12 tests skipped, 900 passed",
])
def test_a_quoted_pytest_summary_is_not_a_denial(summary):
    """The count is data. Reading it as a denial refused a receipt MORE complete than the one
    the gate accepts, and the remedy it prescribed was to delete the number."""
    assert acceptance_contradicted([CLAUSE], _ev(summary)) == {}


@pytest.mark.parametrize("denial", [
    "skipped the Postgres run",
    "I skipped that test",
    "the fleet suite was skipped",
    "cannot run the Postgres engine here",
    "NOT DELIVERED",
])
def test_the_abuse_this_gate_exists_for_still_denies(denial):
    """The half that must not regress. GRPH-945 exists because a receipt read
    "... NOT DELIVERED" and coverage passed."""
    assert acceptance_contradicted([CLAUSE], _ev(denial)), denial


def test_a_real_failure_count_still_denies():
    """`0 failed` is stripped as an honest summary; a non-zero count is not."""
    assert acceptance_contradicted([CLAUSE], _ev("3 failed, 10 passed"))


def test_a_green_run_with_no_skips_is_still_not_denied():
    assert acceptance_contradicted([CLAUSE], _ev("4656 passed, 0 failed")) == {}


# ── the round trip ─────────────────────────────────────────────────────────────────────

def test_compared_and_waived_survive_being_stored():
    """`_normalize_predicates` REBUILDS each row from named fields rather than copying it, so
    a field it does not name is dropped on the way into the database.

    Both halves of the waiver live in fields it did not name. Every test above constructs its
    predicates in memory and so could not see it: the mechanism worked right up until an
    attestation was written and read back, at which point `compared` was gone, the gate saw a
    bare `passed: False`, and the item it was meant to admit was blocked forever."""
    [row] = _normalize_predicates([{
        "name": "commit_is_not_the_base", "passed": False, "detail": "no base",
        "compared": False, "waived": "built by hand, PR #913",
    }])

    assert row["compared"] is False
    assert row["waived"] == "built by hand, PR #913"
    assert len(valid_attestations(_att([row]))) == 1, "the stored row must still be admitted"


def test_a_predicate_that_reports_neither_field_stores_neither():
    """Absent is not `compared: False`. An adapter that does not report whether its check ran
    is making no claim about it, and inventing one would put every CI receipt into the
    could-not-run branch."""
    [row] = _normalize_predicates([{"name": "suite_green", "passed": True, "detail": "CI"}])

    assert "compared" not in row and "waived" not in row


def test_a_waived_reason_that_is_blank_is_not_stored():
    [row] = _normalize_predicates([{
        "name": "x", "passed": False, "detail": "d", "compared": False, "waived": "   "}])

    assert "waived" not in row


# ── the review bounce on PR #921 ───────────────────────────────────────────────────────

def test_a_waived_predicate_does_not_count_as_attested():
    """A waiver admits ONE item past completion. It does not make a required guarantee true.

    `attested_predicates` unioned every name on an admitted receipt, so a waived
    `independent_review` satisfied `missing_predicates([...], ["independent_review"])` and a
    reason string stood in for a check that never ran — the laundering path the waiver is
    explicitly not meant to open (review bounce, PR #921).
    """
    att = _att([_p("suite_green"),
                _p("independent_review", passed=False, compared=False,
                   waived="no built_by on this row")])

    assert len(valid_attestations(att)) == 1, "the item is still admitted"
    assert attested_predicates(att) == {"suite_green"}
    assert missing_predicates(att, ["independent_review"]) == ["independent_review"]


def test_a_passing_predicate_still_counts_as_attested():
    """The control: a filter that counted nothing would satisfy the assertion above."""
    att = _att([_p("suite_green"), _p("independent_review")])

    assert attested_predicates(att) == {"suite_green", "independent_review"}
    assert missing_predicates(att, ["independent_review"]) == []


def test_the_service_refusal_itself_says_how_to_satisfy_it():
    """Pinned at the SERVICE, not through MCP.

    The MCP handler adds its own hint that also mentions `waive`, so an assertion on the
    combined text passes even when the service message says nothing useful — measured: a
    sabotage that stripped `waive` from the service message left the end-to-end test green.
    A caller using the service directly gets only this string.
    """
    from app.services.fleet import UncomparedPredicate, sign_off

    assert UncomparedPredicate.__doc__, "the refusal explains itself to whoever reads the class"
    src = __import__("inspect").getsource(sign_off)
    msg = src[src.index("raise UncomparedPredicate("):]
    msg = msg[:msg.index("\n    )") + 1] if "\n    )" in msg else msg[:600]
    assert "waive=" in msg, "the refusal must name the escape hatch, or it is a wall"
    assert "cut from" in msg, "and name what would let the check actually run"


# ── GRPH-1008, second pass: a denial is made in the same breath as the clause ──────────

def test_a_denial_word_sentences_after_the_clause_is_explanation_not_denial():
    """Both shapes that permanently blocked an item on the live server, verbatim in spirit.
    Evidence is append-only, so a single word of explanation was a life sentence."""
    panel = "the empty and error states use `PlannerStates`, not a bare panel"
    receipt = (f'Clause "{panel}" — held by the web tests (PlannerError copy present, the '
               "empty-state title absent). Deleting the isError branch so a failed read drops "
               "through to the empty state turns 1 test red.")
    assert acceptance_contradicted([panel], [{"kind": "test", "detail": receipt}]) == {}

    table = "§1.3 lists all four, each with its basis and where it is enforced"
    receipt = (f"Clause: '{table}'. Delivered as a four-row table under a new heading. "
               "Verified by a doc-driven claim checker: 36 checks, all resolve. It is not "
               "committed; the four design-project literals it cannot resolve from a checkout "
               "are allowlisted by name with a reason.")
    assert acceptance_contradicted([table], [{"kind": "test", "detail": receipt}]) == {}


def test_a_terse_verdict_in_the_next_sentence_still_denies():
    """The shape the scope must not lose: the clause named, a full stop, then the verdict."""
    assert acceptance_contradicted(
        [CLAUSE], [{"kind": "test", "detail": f"{CLAUSE}. Not delivered."}])
    assert acceptance_contradicted(
        [CLAUSE], [{"kind": "test", "detail": f"Checked {CLAUSE}. Cannot be tested here."}])


def test_a_denial_in_the_same_sentence_still_denies():
    """GRPH-945's own case, which the scope must keep: no full stop between clause and verdict."""
    assert acceptance_contradicted(
        [CLAUSE], [{"kind": "test", "detail": f"{CLAUSE} — NOT DELIVERED, no clock"}])


def test_a_dot_inside_a_filename_is_not_a_sentence_boundary():
    """Receipts are full of `scripts/gen_prd_index.py` and `v0.1`. A scope that split on bare
    dots would end the clause's sentence at the first filename and never reach the verdict.

    The first version of this test was vacuous and the sabotage run said so: its denial sat
    four words after the filename dot, so the terse-next-sentence rule re-included it and the
    mutant passed anyway. The verdict here is a long explanatory clause away from the dot —
    the shape that only a correct boundary rule reaches.
    """
    clause = "the index is regenerated"
    receipt = (f"Clause {clause} — ran scripts/gen_prd_index.py against the live instance and "
               "compared the output of docs/prd-index.json to the committed file and nothing "
               "about the section list had moved, NOT DELIVERED")
    assert acceptance_contradicted([clause], [{"kind": "test", "detail": receipt}])


# ── review bounce on PR #923 ───────────────────────────────────────────────────────────

def test_a_denial_on_a_later_mention_of_the_clause_still_denies():
    """THE bounce. `_clause_scope` scoped only the first mention, so a receipt that named the
    clause twice and attached the verdict to the second one passed — where the whole-line
    scan it replaced had caught it. The reviewer's repro, verbatim."""
    receipt = (f"Clause {CLAUSE}: implemented and verified on SQLite. Re-checked on Postgres: "
               f"{CLAUSE} — NOT DELIVERED, the clock is never armed.")
    assert acceptance_contradicted([CLAUSE], [{"kind": "test", "detail": receipt}])


def test_a_terse_verdict_in_the_sentence_before_the_clause_still_denies():
    """The lesser finding, fixed rather than pinned: the six-word rule runs backwards too."""
    receipt = f"Not delivered. {CLAUSE} is left for a follow-up item."
    assert acceptance_contradicted([CLAUSE], [{"kind": "test", "detail": receipt}])


def test_a_long_explanation_before_the_clause_is_still_not_a_denial():
    """The control for the arm above. The sentence before is explanation, not a verdict, and a
    rule that read it would reopen the blocked-for-a-word hole from the other side."""
    receipt = ("The migration chain was proven from empty on a fresh database, and one row the "
               f"older seed cannot produce was dropped from the fixture. {CLAUSE} — held by "
               "test_pin_lapses, 3 passed.")
    assert acceptance_contradicted([CLAUSE], [{"kind": "test", "detail": receipt}]) == {}
