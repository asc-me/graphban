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

import dataclasses
import io
import json
import time
from pathlib import Path

from gbfleet import adapters
from gbfleet.cli import report
from gbfleet.spawn import Child
from gbfleet.supervisor import Limits, Wave, watch_tick
from gbfleet.until import Report


class _Dead:
    def __init__(self, code: int = 0) -> None:
        self._code = code

    def poll(self) -> int | None:
        return self._code


class _Quiet:
    """The least client `watch_tick` will accept. Nothing here is under test — the reading is
    off the child's own log, not off the server."""

    def fleet_status(self) -> dict:
        return {"agents": []}

    def post_attempt(self, **payload):
        return None

    def call(self, tool, **args):
        return {}


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


# ---- the WIRING, which the PR #904 bounce found had no test at all -----------------------
#
# The reviewer ran four mutations at the call sites and every one stayed green, because
# `substitution` and `cli.report` were tested as units and nothing exercised the path between
# spawn and the wave:
#
#   M1  delete the parser call in _report_exits    -> 5 red, all older gbagent tests
#   M2  facts.setdefault("model", child.model)     -> 101 passed  <- bullet 4's exact concern
#   M3  drop model=launch.model from Child(...)    -> 101 passed
#   M4  substitution(child.model, child.model)     -> 101 passed
#
# M2 is the one that matters: an unreadable stream would silently record the REQUESTED model,
# which is the confident wrong number the item exists to prevent, shipping green.
#
# The fixture is a REAL stream, trimmed from p43-qwen-906's stdout.log — its init event names
# qwen3.7-plus and its result carries 185,200 tokens. The synthetic two-event version could not
# have caught a parser that tripped over the assistant/user events real runs interleave.

FIXTURE = Path(__file__).parent / "fixtures" / "qwen_stdout.json"


def _qwen_child(log_dir: Path, *, model: str, stream: str | None) -> Child:
    """An exited qwen-code child whose stdout.log holds `stream` (None writes no file)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    if stream is not None:
        (log_dir / "stdout.log").write_text(stream)
    return Child(adapter="qwen-code", worktree=Path("/tmp/wt"), branch="gb/q-1", base="",
                 seat_path=Path("/tmp/seat.json"), process=_Dead(0),
                 started_at=time.monotonic() - 30, log_dir=log_dir,
                 binary_version="0.23.0", seat_id=None, model=model)


def test_a_real_stream_reaches_the_wave_and_names_the_substitution(tmp_path: Path):
    """(a) from the bounce. Asked for qwen3.8-max; the recorded stream says qwen3.7-plus, which
    is what 1488 of 1489 real children actually ran."""
    wave = Wave()
    child = _qwen_child(tmp_path / "logs", model="qwen3.8-max", stream=FIXTURE.read_text())

    watch_tick(wave, [child], Limits(), _Quiet(), debug=False)

    assert wave.spend["gb/q-1"]["model"] == "qwen3.7-plus"
    assert wave.spend["gb/q-1"]["tokens_in"] == 184566
    assert wave.substituted["gb/q-1"] == ("qwen3.8-max", "qwen3.7-plus")


def test_an_unreadable_stream_records_no_model_and_no_substitution(tmp_path: Path):
    """(b), and THE ONE THAT KILLS M2. With `facts.setdefault("model", child.model)` the wave
    would record qwen3.8-max — a model that never ran — from a stream nobody could read. A gap
    is the correct output; a confident wrong number is the defect."""
    wave = Wave()
    child = _qwen_child(tmp_path / "logs", model="qwen3.8-max",
                        stream=FIXTURE.read_text()[:120])  # truncated mid-array

    watch_tick(wave, [child], Limits(), _Quiet(), debug=False)

    assert "model" not in wave.spend.get("gb/q-1", {})
    assert "gb/q-1" not in wave.substituted


def test_no_stream_at_all_records_no_model_and_no_substitution(tmp_path: Path):
    """The other absence: a child killed before it wrote anything. 9 of 1567 real logs were
    empty, so this is the ordinary case rather than a corner."""
    wave = Wave()
    child = _qwen_child(tmp_path / "logs", model="qwen3.8-max", stream=None)

    watch_tick(wave, [child], Limits(), _Quiet(), debug=False)

    assert "model" not in wave.spend.get("gb/q-1", {})
    assert "gb/q-1" not in wave.substituted


def test_a_stream_naming_the_model_that_was_asked_for_is_no_substitution(tmp_path: Path):
    """The control, and it kills M4 (`substitution(child.model, child.model)`): agreement must
    record nothing, or every wave would report a substitution against itself."""
    wave = Wave()
    stream = FIXTURE.read_text().replace("qwen3.7-plus", "qwen3.8-max")
    child = _qwen_child(tmp_path / "logs", model="qwen3.8-max", stream=stream)

    watch_tick(wave, [child], Limits(), _Quiet(), debug=False)

    assert wave.spend["gb/q-1"]["model"] == "qwen3.8-max"
    assert "gb/q-1" not in wave.substituted


def test_spawn_carries_the_requested_model_onto_the_child():
    """(c), which kills M3. Without `model=launch.model` the child's request is always "", so
    `substitution` can never fire in a real wave however correct it is in isolation."""
    import inspect

    from gbfleet import spawn as spawn_mod

    src = inspect.getsource(spawn_mod.spawn)
    assert "model=launch.model" in src, "the launch's model is not carried onto the Child"
    # And the field exists to be carried into.
    assert "model" in {f.name for f in dataclasses.fields(Child)}
