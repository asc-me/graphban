"""A dimension clears only if an author answer actually says so (follow-up to PRD-15).

PRD-15 left this open: *"whether a real model's terminal verdict needs a floor beyond
'at least one answer' — a model that is too easily satisfied approves thin specs."*

It does. On the machinery's first real use — grilling PRD-12 — a single answer about
rebaseline approval cleared all four dimensions. The model's own notes claimed the author
had "clearly defined the contracts between components" and given "a clear explanation of
failure modes". The author had said nothing about either.

The cause was subtler than a lenient bar: `grill_context` leads with the PRD body, so the
model read a thorough document and graded THE ARTIFACT rather than THE INTERROGATION. A
well-written PRD was evidence for its own approval.

The fix is a citation the server checks rather than trusts: a verdict must point at a
numbered author answer that exists. The PRD's own prose cannot satisfy that, because the
document is not an answer.

It briefly demanded a verbatim quote too. That was removed — three separate bugs came
from the validator disagreeing with how models render text, each rejecting a CORRECT
verdict, and the quote never bought what people assume: a real quote filed under the
wrong dimension passes it just as easily.

So the honest statement of this floor: it guarantees a verdict points at something the
AUTHOR said. It does NOT guarantee that answer was about that dimension. Misattribution
is a reviewer's job, which is why the note and answer number are recorded where a
reviewer sees them. `test_index_only_does_not_prevent_misattribution` pins that limit so
nobody mistakes this for more than it is.
"""
import json

import pytest

from app.services import prds as prd_svc
from app.services.platform import Resolved


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
    """A PRD whose BODY discusses all four dimensions in detail — the shape that fooled
    the classifier. Anything that clears here must clear on the answers, not the text."""
    return prd_svc.create_prd(
        db, title="Thorough", project_id="core",
        body=("# Thorough\n\n## Scope\nOut of scope: hosted mode, migrations.\n\n"
              "## Failure modes\nOn timeout we retry once then surface the error.\n\n"
              "## Contracts\nJSON-RPC over POST /api/mcp with an X-API-Key header.\n\n"
              "## Open decisions\nPricing is settled; nothing remains open.\n"),
    )


def _model(monkeypatch, payload):
    class _Chat:
        model, base_url = "test-model", "http://x"

        def chat(self, **kw):
            return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", _Chat()))


def _answer(db, prd, text):
    hist = prd_svc.grill_history(db, prd.id)
    prd_svc.record_grill_turns(db, prd.id, hist + [{"role": "user", "text": text}], via="human")


# ---- the failure that actually happened -----------------------------------------------
def test_a_thorough_prd_with_no_answers_resolves_nothing(db, prd, monkeypatch):
    """The original bug, stated correctly for index-only.

    PRD-12 was approved because the classifier read a thorough document and reported that
    the author had explained failure modes and contracts. The author had said nothing.

    Under index-only the document cannot be cited at all — it is not a numbered answer —
    so a PRD with no answers resolves nothing no matter how complete its prose. The
    `prd` fixture here deliberately covers all four dimensions in its body."""
    _model(monkeypatch, {n: {"outcome": "resolved", "note": "the PRD covers this thoroughly",
                             "answered_by": 1} for n in prd_svc.DIMENSIONS})
    prd_svc.classify_grill(db, prd)

    done = prd_svc.completion(db, prd.id)
    assert done["complete"] is False
    assert sorted(done["outstanding"]) == sorted(prd_svc.DIMENSIONS)


def test_index_only_does_not_prevent_misattribution(db, prd, monkeypatch):
    """The limit of this design, pinned deliberately rather than discovered later.

    One answer CAN clear four dimensions, because a valid index is all the server can
    check. The quote requirement did not prevent this either — grading PRD-12, the model
    quoted real words from a real answer and filed them under three dimensions the answer
    never addressed. Verifying relevance is the same judgement we are asking the model to
    make, so no server-side check reaches it.

    What the recorded note and answer number buy is that a reviewer can SEE it in one
    glance, which is the achievable property."""
    _answer(db, prd, "Rebaselines are approved by grilling, same as the PRD itself.")
    _model(monkeypatch, {n: {"outcome": "resolved", "note": "n", "answered_by": 1}
                         for n in prd_svc.DIMENSIONS})
    prd_svc.classify_grill(db, prd)

    done = prd_svc.completion(db, prd.id)
    assert done["complete"] is True, "index-only accepts this; the reviewer catches it"
    assert all("from answer 1" in d["note"] for d in done["dimensions"].values()), \
        "so every verdict must say WHICH answer it leant on"


def test_a_real_answer_still_resolves_its_dimension(db, prd, monkeypatch):
    """The floor must not make approval unreachable — it makes it earned."""
    _answer(db, prd, "Out of scope for v1: hosted mode and any migration tooling.")
    _model(monkeypatch, {"scope_edges": {"outcome": "resolved", "note": "author set the edges",
                                         "answered_by": 1}})
    prd_svc.classify_grill(db, prd)

    d = prd_svc.completion(db, prd.id)["dimensions"]["scope_edges"]
    assert d["outcome"] == "resolved"
    assert "from answer 1" in d["note"]


def test_a_deferral_must_be_cited_too(db, prd, monkeypatch):
    """Deferring is the author's decision, so it needs the author's words behind it —
    otherwise a model could defer everything and complete the grill."""
    _answer(db, prd, "Let's leave pricing open until after the beta.")
    _model(monkeypatch, {
        "open_decisions": {"outcome": "deferred", "note": "author deferred", "answered_by": 1},
        # No answer 7 exists — a deferral cannot be conjured out of nothing.
        "contracts": {"outcome": "deferred", "note": "invented", "answered_by": 7},
    })
    prd_svc.classify_grill(db, prd)

    dims = prd_svc.completion(db, prd.id)["dimensions"]
    assert dims["open_decisions"]["outcome"] == "deferred"
    assert dims["contracts"]["outcome"] == "unanswered"


# ---- malformed citations fail closed ----------------------------------------------------
@pytest.mark.parametrize("entry,why", [
    ({"outcome": "resolved", "note": "n"}, "no citation at all"),
    ({"outcome": "resolved", "note": "n", "answered_by": 9}, "answer does not exist"),
    ({"outcome": "resolved", "note": "n", "answered_by": "one"}, "unparseable index"),
])
def test_a_broken_citation_is_unanswered(db, prd, monkeypatch, entry, why):
    _answer(db, prd, "Out of scope for v1: hosted mode.")
    _model(monkeypatch, {"scope_edges": entry})
    prd_svc.classify_grill(db, prd)
    assert prd_svc.completion(db, prd.id)["dimensions"]["scope_edges"]["outcome"] == "unanswered", why


def test_no_answers_means_the_model_is_not_even_asked(db, prd, monkeypatch):
    """Nothing is citable, so there is nothing to grade. Saves a call and removes the
    only situation where a model could invent a citation with no source to check it."""
    called = []

    class _Chat:
        model, base_url = "m", "http://x"

        def chat(self, **kw):
            called.append(1)
            return "{}"

    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", _Chat()))
    prd_svc.classify_grill(db, prd)
    assert called == []
    assert prd_svc.completion(db, prd.id)["complete"] is False


# ---- the prompt must ask for the dimensions it grades ---------------------------------
def test_the_classify_prompt_names_every_dimension():
    """Caught in production, on PRD-12. The prompt said "four fixed dimensions" and named
    none of them, so the model invented its own from the document's subject matter
    (`intent_baseline`, `coverage`). None matched `DIMENSIONS`, every verdict was
    discarded, and grading silently fell back to the stub — a real model configured and
    paid for, contributing nothing.

    The prompt is now BUILT from `DIMENSIONS`; this asserts it stays that way, because a
    prompt that drifts from the standard it grades fails invisibly."""
    for name, question in prd_svc.DIMENSIONS.items():
        assert name in prd_svc.GRILL_CLASSIFY_SYSTEM, f"prompt never names {name}"
        assert question in prd_svc.GRILL_CLASSIFY_SYSTEM, f"prompt omits what {name} means"


def test_verdicts_under_unknown_keys_are_ignored(db, prd, monkeypatch):
    """The failure mode itself: a reply keyed by invented names must not resolve anything,
    and must not crash."""
    _answer(db, prd, "Out of scope for v1: hosted mode.")
    _model(monkeypatch, {
        "intent_baseline": {"outcome": "resolved", "note": "n", "answered_by": 1},
        "coverage": {"outcome": "resolved", "note": "n", "answered_by": 1},
    })
    prd_svc.classify_grill(db, prd)

    done = prd_svc.completion(db, prd.id)
    assert sorted(done["outstanding"]) == sorted(prd_svc.DIMENSIONS)


# ---- quoting the way people actually quote ---------------------------------------------