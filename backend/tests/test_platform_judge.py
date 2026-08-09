"""The platform judge: completed work against the PRD goal (GRPH-249 / PRD-12).

Three answers come from three different places, deliberately:

- **serves / unrelated** is the semantic call, and the only part a model is asked.
- **enables** is DERIVED from the link graph. Typed links already encode it, so asking a
  model to re-derive it would be slower, less reliable, and would spend the one expensive
  call on a question already answered.
- **undecidable** is what an unconfigured instance returns — `chat_provider` defaults to
  `stub`, and guessing there would put a clean bill of health on an instance that judged
  nothing.

The judge's ceiling is bounded and stated: it sees item text, evidence and touchpoints,
never a diff. `serves` means "this is about the right thing", never "this works".
"""
import json

import pytest

from app.services import items as items_svc
from app.services import links as links_svc
from app.services import prds as prd_svc

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Goals\n\nJudge completed work against the goal that justified it.\n\n"
    "## Judging\n\nClassify each completed item.\n"
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _approve(db, prd):
    window = prd_svc.grill_window(db, prd.id)
    prior = prd_svc.grill_history(db, prd.id, since=window)
    prd_svc.record_grill_turns(db, prd.id, prior + [
        {"role": "user", "text": f"Answer, round {len(prd_svc.baseline_chain(db, prd.id))}."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    return _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))


@pytest.fixture()
def judge(monkeypatch):
    """A configured judge whose reply is scripted. Without one the project is `stub` and
    every classification is `undecidable`, so the semantic path would never be exercised."""
    from app.services import platform as platform_svc

    state = {"outcome": "serves", "confidence": 0.95, "reasoning": "It advances the goal.",
             "raw": None, "calls": 0, "boom": False}

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            state["calls"] += 1
            state["last_context"] = context
            state["last_temperature"] = temperature
            if state["boom"]:
                raise RuntimeError("judge is down")
            return state["raw"] if state["raw"] is not None else json.dumps({
                "outcome": state["outcome"], "confidence": state["confidence"],
                "reasoning": state["reasoning"]})

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: ("openai", Chat()))
    return state


def _done(db, prd, section="Judging", title="Work", **kw):
    item = items_svc.create_item(db, title=title, project_id="core",
                                 prd_id=prd.id, prd_section=section, **kw)
    items_svc.update_item(db, item.id, status="done")
    db.refresh(item)
    return item


def _row(db, item):
    return prd_svc.classify_work(db, item)


# ---- it fires on completion, not on link -------------------------------------------------
def test_linking_an_item_does_not_classify_it(db, approved, judge):
    """At link time an item is an INTENTION with nothing delivered to judge — a judgement
    of it is a judgement of a sentence somebody typed."""
    items_svc.create_item(db, title="Planned", project_id="core",
                          prd_id=approved.id, prd_section="Judging")

    assert judge["calls"] == 0
    assert prd_svc.classifications(db, approved) == []


def test_classifying_unfinished_work_is_refused_at_the_judge_itself(db, approved, judge):
    """The completion trigger is one guard; this is the other. Testing only the caller
    leaves the judge itself willing to grade an intention, and the next caller inherits
    that — which is how "fires on completion" quietly becomes "fires whenever asked"."""
    item = items_svc.create_item(db, title="Planned", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")

    assert prd_svc.classify_work(db, item) is None
    assert judge["calls"] == 0


def test_completing_an_item_classifies_it(db, approved, judge):
    item = _done(db, approved)

    rows = prd_svc.classifications(db, approved)
    assert [r["item"] for r in rows] == [item.key]
    assert rows[0]["outcome"] == "serves"


def test_the_classification_is_stamped_with_the_baseline_it_judged(db, approved, judge):
    """A baseline change invalidates prior judgements, so a row with no baseline stamped
    could never be known to be current."""
    assert _row(db, _done(db, approved)).baseline_version == "v1.0"


def test_the_judge_is_asked_at_temperature_zero(db, approved, judge):
    """Judging is not writing. A classifier that returns a different verdict for identical
    input makes the answer depend on when it ran — the same fix as the grill's."""
    _done(db, approved)
    assert judge["last_temperature"] == 0


def test_the_goal_leads_the_context_and_the_work_follows(db, approved, judge):
    """Opposite order to the grill's classifier, and for the opposite reason: there the
    risk was grading the document instead of the interrogation; here the document IS the
    standard."""
    _done(db, approved)
    ctx = judge["last_context"]

    assert ctx.index("PRD GOAL") < ctx.index("COMPLETED WORK")
    assert "Judge completed work against the goal" in ctx


# ---- enables is derived, never judged ------------------------------------------------------
def test_unrelated_work_that_unblocks_the_goal_is_reclassified_as_enables(db, approved, judge):
    """Work that unblocks work serving the goal is not unrelated to it, whatever it looks
    like read on its own. Derived from typed links rather than asked of the model — the
    graph already holds the answer, and the one expensive call is spent elsewhere."""
    served = _done(db, approved, title="Serves it")
    judge["outcome"], judge["confidence"] = "unrelated", 0.95
    plumbing = _done(db, approved, title="Groundwork")
    links_svc.create_link(db, a=plumbing.id, b=served.id, type_="dependency",
                          reason="unblocks", project_id="core")

    row = _row(db, plumbing)
    prd_svc.classify_work(db, plumbing, force=True)
    db.refresh(row)
    assert row.outcome == "enables" and "link graph" in row.reasoning


def test_the_model_is_never_asked_for_enables(db, approved, judge):
    """The prompt forbids it, and an `enables` reply would be rejected as unusable —
    keeping one source of truth for a fact the graph already holds."""
    assert "Do NOT answer `enables`" in prd_svc.JUDGE_SYSTEM

    judge["raw"] = json.dumps({"outcome": "enables", "confidence": 0.9, "reasoning": "x"})
    assert _row(db, _done(db, approved)).outcome == "undecidable"


def test_a_dependency_on_another_prd_does_not_make_it_enables(db, approved, judge):
    """Enabling has to be enabling THIS goal. Any dependency counting would make the label
    fire constantly and stop meaning anything."""
    other = _approve(db, prd_svc.create_prd(db, title="Elsewhere", project_id="core",
                                            body=BODY))
    elsewhere = _done(db, other, title="Other PRD work")
    judge["outcome"], judge["confidence"] = "unrelated", 0.95
    stray = _done(db, approved, title="Stray")
    links_svc.create_link(db, a=stray.id, b=elsewhere.id, type_="dependency",
                          reason="unblocks", project_id="core")

    assert _row(db, stray).outcome == "unrelated"


# ---- confidence gates the consequence, not the answer ---------------------------------------
def test_a_confident_unrelated_self_flags(db, approved, judge):
    judge["outcome"], judge["confidence"] = "unrelated", 0.95
    row = _row(db, _done(db, approved))

    assert row.outcome == "unrelated" and row.needs_review is False


def test_an_uncertain_unrelated_defers_to_signoff(db, approved, judge):
    """AL-227's triage shape, one domain over. The ambiguous middle is recorded but not
    acted on — a confident wrong answer is worse than an admitted uncertain one."""
    judge["outcome"], judge["confidence"] = "unrelated", 0.4
    row = _row(db, _done(db, approved))

    assert row.outcome == "unrelated" and row.needs_review is True


# ---- it degrades rather than guesses ----------------------------------------------------------
def test_with_no_provider_the_answer_is_undecidable(db, approved):
    """`chat_provider` defaults to `stub`. Guessing here would put a clean bill of health
    on an instance that judged nothing."""
    row = _row(db, _done(db, approved))

    assert row.outcome == "undecidable" and row.graded_by == "stub"
    assert row.needs_review is True, "an unjudged item must not pass silently"


def test_a_judge_that_errors_records_undecidable_rather_than_a_guess(db, approved, judge):
    judge["boom"] = True
    assert _row(db, _done(db, approved)).outcome == "undecidable"


def test_an_unparseable_reply_is_undecidable(db, approved, judge):
    judge["raw"] = "I think it's probably fine, honestly"
    assert _row(db, _done(db, approved)).outcome == "undecidable"


def test_a_failing_judge_never_blocks_completion(db, approved, judge):
    """The classification is a read on the work, not a gate on it. Making delivery depend
    on a model being reachable is how a feature gets routed around."""
    judge["boom"] = True
    item = items_svc.create_item(db, title="Ships anyway", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")

    items_svc.update_item(db, item.id, status="done")
    db.refresh(item)
    assert item.status == "done"


def test_completion_survives_the_judge_raising_outright(db, approved, monkeypatch):
    """The test above only proves the INNER guard: `classify_work` catches a chat failure
    itself and records `undecidable`, so the completion hook's own try/except is never
    reached. This forces the outer one — a bug in classification, a schema error, anything
    unanticipated — and the completion must still stand."""
    def boom(*a, **k):
        raise RuntimeError("classification blew up entirely")

    monkeypatch.setattr(prd_svc, "classify_work", boom)
    item = items_svc.create_item(db, title="Ships anyway", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")

    items_svc.update_item(db, item.id, status="done")
    db.refresh(item)
    assert item.status == "done"


def test_work_on_a_prd_with_no_baseline_is_not_judged(db, judge):
    """No agreed intent to judge against. Judging against an unapproved draft would make
    the standard whatever the body said this morning."""
    draft = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    item = items_svc.create_item(db, title="Work", project_id="core",
                                 prd_id=draft.id, prd_section="Judging")
    items_svc.update_item(db, item.id, status="done")
    db.refresh(item)

    assert prd_svc.classify_work(db, item) is None and judge["calls"] == 0


# ---- staleness --------------------------------------------------------------------------------
def test_a_rebaseline_marks_every_classification_stale(db, approved, judge):
    """Every judgement was made against intent that has just been superseded."""
    for i in range(5):  # above STALE_INLINE_MAX, so nothing recomputes inline
        _done(db, approved, title=f"Work {i}")
    judge["calls"] = 0

    prd_svc.request_rebaseline(db, approved, reason_type="learning", reason="Moved.",
                               requested_by="agent:t")
    prd_svc.update_prd(db, approved.id, body=BODY.replace("Classify each completed item.",
                                                          "Rewritten."))
    _approve(db, approved)

    rows = prd_svc.classifications(db, approved, refresh=False)
    assert all(r["stale"] for r in rows)
    assert judge["calls"] == 0, "a large set must not recompute inline"


def test_a_small_stale_set_recomputes_inline(db, approved, judge):
    """The eager path is a WARM-UP on the lazy one, not a second design — so both agree by
    construction. Three is a named constant, deliberately conservative, and not a setting."""
    assert prd_svc.STALE_INLINE_MAX == 3
    for i in range(2):
        _done(db, approved, title=f"Work {i}")
    judge["calls"] = 0

    prd_svc.request_rebaseline(db, approved, reason_type="learning", reason="Moved.",
                               requested_by="agent:t")
    prd_svc.update_prd(db, approved.id, body=BODY.replace("Classify each completed item.",
                                                          "Rewritten."))
    _approve(db, approved)

    assert judge["calls"] == 2
    rows = prd_svc.classifications(db, approved, refresh=False)
    assert not any(r["stale"] for r in rows)
    assert all(r["baseline_version"] == "v1.1" for r in rows)


def test_reading_the_report_refreshes_a_stale_set(db, approved, judge):
    """The lazy half: reading is what pays for a large rebaseline's recompute, so the write
    path stays fast and the numbers a reader sees are never quietly out of date."""
    for i in range(5):
        _done(db, approved, title=f"Work {i}")
    prd_svc.request_rebaseline(db, approved, reason_type="learning", reason="Moved.",
                               requested_by="agent:t")
    prd_svc.update_prd(db, approved.id, body=BODY.replace("Classify each completed item.",
                                                          "Rewritten."))
    _approve(db, approved)

    rows = prd_svc.classifications(db, approved)  # refresh=True by default
    assert not any(r["stale"] for r in rows)
    assert all(r["baseline_version"] == "v1.1" for r in rows)


def test_a_fresh_classification_is_not_re_asked(db, approved, judge):
    """The one expensive call per item, per baseline. Re-asking on every read would make
    opening a report cost a model call per completed item."""
    item = _done(db, approved)
    before = judge["calls"]

    prd_svc.classify_work(db, item)
    prd_svc.classify_work(db, item)
    assert judge["calls"] == before
