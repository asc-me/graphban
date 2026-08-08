"""A verdict's citation points at the answer that settled it (GRPH-323 / PRD-15).

Found while re-grilling PRD-12. All four dimensions came back `turn_seq: 3` while their
notes cited four *different* answers — because `classify_grill` passed
`len(history) - 1` as `turn_seq` for every dimension. That is not a citation; it is where
the transcript ended when grading ran.

The model's real citation, `answered_by`, is validated (an out-of-range index downgrades
the dimension to `unanswered` — the PR #118 floor, working). It was then folded into the
note as prose and never reached the structured field.

Three index spaces are in play, and the wrong one was stored:

  answered_by      1-based into the ANSWERS of the current window
  len(history)-1   0-based into all TURNS of the current window
  GrillTurn.seq    absolute across the whole transcript, what a reader resolves against

This matters because PR #122 dropped quote-matching *for* index-only citations, on the
grounds that the index is what makes a verdict resolvable. That trade only pays off if the
stored index resolves.

**These tests assert the indices DIFFER and each resolves to the right turn.** Asserting
`turn_seq is not None` passes against the defect.
"""
import pytest

from app.services import prds as prd_svc

BODY = "# Spec\n\n## Problem\n\nNothing checks delivery.\n\n## Judging\n\nClassify it.\n"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def prd(db):
    return prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY)


def _answers(db, prd, n=4):
    """n distinct answers, interleaved with questions so answer index != turn index —
    which is the whole point: a citation that only works when they coincide is not one."""
    turns = []
    for i in range(n):
        turns.append({"role": "agent", "text": f"Question {i}?"})
        turns.append({"role": "user", "text": f"Answer number {i} about dimension {i}."})
    prd_svc.record_grill_turns(db, prd.id, turns)
    return turns


def _seq_of(db, prd, answer_index: int) -> int:
    """The global seq of the nth (1-based) answer."""
    answers = [t for t in prd_svc.grill_turns(db, prd.id) if t.role == "user"]
    return answers[answer_index - 1].seq


def _fake_grader(monkeypatch, mapping: dict[str, int]):
    """A real (non-stub) grader whose verdicts cite the answers in `mapping`."""
    import json

    from app.services import platform as platform_svc

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            return json.dumps({
                name: {"outcome": "resolved", "note": f"settled by {idx}", "answered_by": idx}
                for name, idx in mapping.items()
            })

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("openai", Chat()))


# ---- the defect ------------------------------------------------------------------------
def test_each_dimension_cites_the_answer_that_settled_it(db, prd, monkeypatch):
    """THE test. Before the fix every dimension carried the same `turn_seq` — the last
    turn — regardless of which answer the verdict named."""
    _answers(db, prd)
    _fake_grader(monkeypatch, {"scope_edges": 1, "failure_modes": 2,
                               "contracts": 3, "open_decisions": 4})

    dims = prd_svc.classify_grill(db, prd)["dimensions"]
    got = {n: d["turn_seq"] for n, d in dims.items()}

    assert got == {
        "scope_edges": _seq_of(db, prd, 1),
        "failure_modes": _seq_of(db, prd, 2),
        "contracts": _seq_of(db, prd, 3),
        "open_decisions": _seq_of(db, prd, 4),
    }
    assert len(set(got.values())) == 4, "four distinct citations collapsed to one turn"


def test_the_citation_resolves_to_the_text_the_note_names(db, prd, monkeypatch):
    """Falsifiability is the point: a reader must be able to fetch the cited turn and see
    the answer the verdict claims settled it."""
    _answers(db, prd)
    _fake_grader(monkeypatch, {"scope_edges": 2, "failure_modes": 2,
                               "contracts": 2, "open_decisions": 2})

    seq = prd_svc.classify_grill(db, prd)["dimensions"]["scope_edges"]["turn_seq"]
    turn = [t for t in prd_svc.grill_turns(db, prd.id) if t.seq == seq][0]

    assert turn.role == "user"
    assert turn.text == "Answer number 1 about dimension 1."  # the 2nd answer, 0-indexed body


def test_the_stored_seq_is_absolute_not_an_index_into_the_window(db, prd, monkeypatch):
    """After a rebaseline the window is non-zero, so a window-relative index resolves to
    the wrong turn — or to none. The seq has to be the one `GrillTurn` actually carries."""
    _answers(db, prd, n=2)
    _fake_grader(monkeypatch, {"scope_edges": 1, "failure_modes": 2})
    prd_svc.classify_grill(db, prd)
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)

    prd_svc.request_rebaseline(db, prd, reason_type="learning", reason="Learned.",
                               requested_by="agent:t")
    window = prd_svc.grill_window(db, prd.id)
    assert window > 0, "fixture is inert without a moved window"

    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "A new answer."}])
    _fake_grader(monkeypatch, {"scope_edges": 1})
    seq = prd_svc.classify_grill(db, prd)["dimensions"]["scope_edges"]["turn_seq"]

    assert seq >= window, "citation points before the window it was graded in"
    assert [t for t in prd_svc.grill_turns(db, prd.id) if t.seq == seq][0].text == "A new answer."


# ---- refusing to point at the wrong thing -----------------------------------------------
def test_an_unanswered_dimension_carries_no_citation(db, prd, monkeypatch):
    import json

    from app.services import platform as platform_svc

    _answers(db, prd, n=1)

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            return json.dumps({"scope_edges": {"outcome": "unanswered", "note": "nothing said"}})

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("openai", Chat()))

    dims = prd_svc.classify_grill(db, prd)["dimensions"]
    assert dims["scope_edges"]["outcome"] == "unanswered"
    assert dims["scope_edges"]["turn_seq"] is None


def test_an_out_of_range_citation_is_refused_by_the_floor(db, prd, monkeypatch):
    """The PR #118 citation floor, still doing its job: a verdict naming an answer that
    does not exist is downgraded rather than recorded with a dangling pointer."""
    _answers(db, prd, n=2)
    _fake_grader(monkeypatch, {"scope_edges": 99})

    dims = prd_svc.classify_grill(db, prd)["dimensions"]
    assert dims["scope_edges"]["outcome"] == "unanswered"
    assert dims["scope_edges"]["turn_seq"] is None


# ---- the stub grader cites too ------------------------------------------------------------
def test_the_stub_bar_cites_the_answer_its_rule_maps_to(db, prd):
    """The stub resolves the first N dimensions in order, so dimension i WAS settled by
    answer i — that is the rule, not a guess. Leaving it uncited would make the offline
    path the one place a verdict points nowhere."""
    _answers(db, prd, n=2)  # stub is the default provider in tests

    dims = prd_svc.classify_grill(db, prd)["dimensions"]
    names = list(prd_svc.DIMENSIONS)

    assert dims[names[0]]["turn_seq"] == _seq_of(db, prd, 1)
    assert dims[names[1]]["turn_seq"] == _seq_of(db, prd, 2)
    assert dims[names[2]]["outcome"] == "unanswered"
    assert dims[names[2]]["turn_seq"] is None
