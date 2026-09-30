"""GRPH-982 — the child's own record was written and never read.

**Accept:** `result_facts` parses qwen's `-o json` stream for its token usage and the model
that ANSWERED; a truncated or absent stream yields `{}` rather than zeros; and a model nobody
asked for is reported as a substitution in both the wave JSON and the operator summary.

**What went wrong.** The mechanism already existed — PRD-38 D3's `result_facts` — and only
`gbagent` implemented it. So a qwen wave reported `tokens: 0, unreported: N` while the numbers
sat in `stdout.log`. Across 1489 real children:

  * **436,054,029 tokens** unreported. One child alone spent 12,170,125.
  * `init.model` said `qwen3.7-plus` 1488 times and `qwen3.8-max` once, while every measured
    cell in the preference matrix is filed under `alibaba` + model `""` — so 123 cells could
    not tell two models of one vendor apart.

And the requested model is not the answer: `-m bogus-name` runs the configured default with no
warning anywhere. Measured on 0.23.0:

    -m qwen3.8-max                 ->  init.model = "qwen3.8-max"
    -m definitely-not-a-model-zzz  ->  init.model = "qwen3.7-plus"

Recording the request as measurement is worse than recording nothing: it puts a confident wrong
number in the matrix instead of a gap.
"""
from __future__ import annotations

import io
import json

from gbfleet import adapters
from gbfleet.cli import report
from gbfleet.supervisor import Wave
from gbfleet.until import Report


def _stream(*, model: str = "qwen3.8-max", tokens_in: int = 951247,
            tokens_out: int = 409, turns: int = 15) -> str:
    return json.dumps([
        {"type": "system", "subtype": "init", "model": model, "session_id": "s"},
        {"type": "result", "subtype": "success", "num_turns": turns,
         "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                   "total_tokens": tokens_in + tokens_out}},
    ])


# ---- the reading -------------------------------------------------------------------------

def test_the_tokens_are_read_out_of_the_stream():
    """THE 436M. Every one of those tokens was on disk while its wave reported zero."""
    got = adapters.result_facts("qwen-code", _stream())

    assert got["tokens_in"] == 951247
    assert got["tokens_out"] == 409
    assert got["turns_used"] == 15


def test_the_model_that_answered_is_read_not_the_one_requested():
    """`result_facts` sees only the stream, so it cannot report a request even by accident —
    which is the property that makes it trustworthy for the matrix."""
    assert adapters.result_facts("qwen-code", _stream(model="qwen3.7-plus"))["model"] \
        == "qwen3.7-plus"


def test_an_absent_stream_is_not_zero():
    """`{}` leaves the ledger's fields NULL, which renders as "not reported". A zero would say
    the run was free — the absence-reads-as-clean shape, in the one table whose purpose is to
    be checkable."""
    assert adapters.result_facts("qwen-code", "") == {}
    assert adapters.result_facts("qwen-code", "   ") == {}


def test_a_truncated_stream_is_not_zero():
    """69 of 1567 real logs were truncated — children killed mid-write. Half a JSON array is
    not a measurement of anything."""
    assert adapters.result_facts("qwen-code", _stream()[:60]) == {}


def test_a_stream_that_is_not_an_array_is_refused():
    """The vendor could change shape. Guessing at a new one would put invented numbers in the
    ledger, so an unrecognised shape reports nothing."""
    assert adapters.result_facts("qwen-code", '{"usage": {"input_tokens": 5}}') == {}


def test_an_unmeasured_vendor_still_reports_nothing():
    """The control: adding qwen must not make every adapter claim numbers it cannot read."""
    assert adapters.result_facts("claude", _stream()) == {}


def test_the_last_usage_record_wins():
    """A resumed run describes the same attempt twice and the later record is the one that
    finished — the rule gbagent's reader already states, kept identical here."""
    events = json.loads(_stream(tokens_in=1))
    events.append({"type": "result", "usage": {"input_tokens": 99, "output_tokens": 1}})

    assert adapters.result_facts("qwen-code", json.dumps(events))["tokens_in"] == 99


# ---- the substitution, and its two call sites ---------------------------------------------

def test_only_two_known_and_different_models_are_a_substitution():
    """The polarity, against the real predicate. My first version of this test asserted a
    tautology about its own local variables and reached none of the code — so the decision was
    extracted into `substitution` for the test to address.

    "We could not read the stream" is not "it ran something else", and reporting it as one
    would manufacture a finding out of an absence, which is the defect this item belongs to."""
    from gbfleet.supervisor import substitution

    assert substitution("qwen3.8-max", "qwen3.7-plus") == ("qwen3.8-max", "qwen3.7-plus")

    assert substitution("qwen3.8-max", "qwen3.8-max") is None, "agreement"
    assert substitution("", "qwen3.7-plus") is None, "no request to break"
    assert substitution("qwen3.8-max", "") is None, "the stream said nothing"
    assert substitution(None, None) is None
    assert Wave().substituted == {}, "present and empty by default"


def test_the_json_report_always_carries_the_key():
    """Empty is "none observed", not "not checked". The GRPH-987 bounce was exactly this claim
    going unpinned one layer up, so it is pinned here before anyone asks."""
    assert Report(ok=True, reason="idle", exit=0, wave=Wave()).as_json()["substituted"] == {}
    assert Report(ok=True, reason="idle", exit=0, wave=None).as_json()["substituted"] == {}

    wave = Wave()
    wave.substituted = {"gb/w-1": ("qwen3.8-max", "qwen3.7-plus")}
    got = Report(ok=True, reason="idle", exit=0, wave=wave).as_json()["substituted"]
    assert got == {"gb/w-1": ["qwen3.8-max", "qwen3.7-plus"]}


def test_the_operator_summary_names_both_models_and_who_owns_the_cell():
    """CALL-side. The JSON being right does not help whoever is watching the terminal, and the
    actionable part is which model the measurement belongs to."""
    wave = Wave()
    wave.substituted = {"gb/w-1": ("qwen3.8-max", "qwen3.7-plus")}
    out = io.StringIO()

    report(wave, out=out)
    text = out.getvalue()

    assert "SUBSTITUTED gb/w-1" in text
    assert "qwen3.8-max" in text and "qwen3.7-plus" in text
    assert "belongs to qwen3.7-plus" in text


def test_the_summary_is_silent_on_a_clean_wave():
    """The other half — without it the test above could be satisfied by a line that always
    prints, and a standing "SUBSTITUTED: none" trains an operator to skip the line."""
    out = io.StringIO()
    report(Wave(), out=out)

    assert "SUBSTITUTED" not in out.getvalue()
