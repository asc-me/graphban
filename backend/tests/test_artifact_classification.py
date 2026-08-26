"""What a promoted lesson should become (GRPH-307 / PRD-16).

The step between "this is true" and "here is a thing you can install". Memory answers the
first; nothing answered the second, so a corpus of correct lessons stayed a corpus.

Every test here is really about not producing work a reviewer has to undo:

- two lessons on one subject must become ONE recommendation, not two competing creates;
- a row a human has already answered must never be re-asked;
- a batch that fails must not cost the other batches their work.
"""
import json

import pytest

from app.models import ArtifactRecommendation
from app.services import artifacts as art_svc
from app.services import memory as mem_svc
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
def model(monkeypatch):
    """A configured classifier whose verdicts are scripted. Without one the project is
    `stub` and classification declines to guess, so nothing would be exercised."""
    from app.services import platform as platform_svc

    state = {"verdicts": [], "calls": 0, "raw": None, "boom": False, "batches": []}

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            state["calls"] += 1
            state["batches"].append(context)
            if state["boom"]:
                raise RuntimeError("classifier down")
            if state["raw"] is not None:
                return state["raw"]
            ids = [l["id"] for l in json.loads(context.split("LESSONS:\n")[1])]
            out = []
            for i, lid in enumerate(ids):
                v = dict(state["verdicts"][i % len(state["verdicts"])]) if state["verdicts"] \
                    else {"tier": "fact", "scope": "misc", "title": "t"}
                v["id"] = lid
                out.append(v)
            return json.dumps(out)

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    return state


@pytest.fixture()
def proj(db):
    """A project of its own. The seeded prototype dataset ships published shards, and
    classifying against `core` would mean every assertion here counted them too — the
    fixture would be measuring the seed rather than the behaviour."""
    from app.models import Project

    db.add(Project(id="artifacts", name="Artifacts", tag="AR"))
    db.commit()
    return "artifacts"


def _lesson(db, text, i=0, project="artifacts"):
    return mem_svc.add_memory(db, text_body=f"{text} ({i})", project_id=project,
                              status="published")


# ---- one subject, one recommendation ------------------------------------------------------
def test_two_lessons_on_one_subject_produce_one_recommendation(db, model, proj):
    """THE acceptance criterion. A reviewer handed two creates for one subject has to work
    out which to take, and both would install a file doing the same job."""
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "Bump the range"}]
    _lesson(db, "Always bump the migration range in AGENTS.md", 1)
    _lesson(db, "The migration guard fires when the range is stale", 2)

    art_svc.classify(db, proj)

    assert len(art_svc.pending(db, proj)) == 1


def test_the_second_supersedes_the_first_rather_than_competing(db, model, proj):
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    _lesson(db, "One", 1)
    art_svc.classify(db, proj)
    first = art_svc.pending(db, proj)[0]

    _lesson(db, "Two", 2)
    art_svc.classify(db, proj)

    live = art_svc.pending(db, proj)
    assert len(live) == 1 and live[0].supersedes_id == first.id
    assert db.get(ArtifactRecommendation, first.id).status == "superseded"


def test_the_superseding_row_carries_the_earlier_evidence(db, model, proj):
    """The drafting step re-renders from the CURRENT lesson set, so a superseding row that
    forgot its predecessors would render a weaker artifact than the one it replaced."""
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    a = _lesson(db, "One", 1)
    art_svc.classify(db, proj)
    b = _lesson(db, "Two", 2)
    art_svc.classify(db, proj)

    assert art_svc.pending(db, proj)[0].lesson_ids == sorted([a.id, b.id])


def test_scopes_that_differ_only_in_phrasing_still_collide(db, model, proj):
    """Exact string equality is a check that never fires — a model produces "migration
    guard" and "the migration guards" for the same subject roughly always."""
    _lesson(db, "One", 1)
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    art_svc.classify(db, proj)

    _lesson(db, "Two", 2)
    model["verdicts"] = [{"tier": "rule", "scope": "The Migration Guards", "title": "x"}]
    art_svc.classify(db, proj)

    assert len(art_svc.pending(db, proj)) == 1


def test_different_subjects_stay_separate(db, model, proj):
    """The deduplication must not collapse everything into one artifact — that would be the
    same failure pointing the other way."""
    _lesson(db, "One", 1)
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    art_svc.classify(db, proj)

    _lesson(db, "Two", 2)
    model["verdicts"] = [{"tier": "rule", "scope": "vector index", "title": "y"}]
    art_svc.classify(db, proj)

    assert len(art_svc.pending(db, proj)) == 2


def test_the_same_scope_on_a_different_tier_is_a_different_artifact(db, model, proj):
    """A rule and a hook about one subject are two things — one is read, one runs."""
    _lesson(db, "One", 1)
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    art_svc.classify(db, proj)

    _lesson(db, "Two", 2)
    model["verdicts"] = [{"tier": "hook", "scope": "migration guard", "title": "y"}]
    art_svc.classify(db, proj)

    assert len(art_svc.pending(db, proj)) == 2


# ---- a human's decision is never re-asked ---------------------------------------------------
def test_a_reviewed_recommendation_is_not_flipped_back_to_queued(db, model, proj):
    """The serious half of "only classify what has never been classified". A human said no,
    and a later run would quietly ask again as though they had not."""
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "x"}]
    _lesson(db, "One", 1)
    art_svc.classify(db, proj)
    rec = art_svc.pending(db, proj)[0]
    rec.status = "rejected"
    db.commit()

    _lesson(db, "Two", 2)
    art_svc.classify(db, proj)

    assert db.get(ArtifactRecommendation, rec.id).status == "rejected"


def test_a_lesson_that_already_has_a_recommendation_is_not_reclassified(db, model, proj):
    """Re-running over the full set burns provider quota for answers nobody asked for
    again — which is how a scheduled job gets switched off."""
    model["verdicts"] = [{"tier": "fact", "scope": "a", "title": "x"}]
    _lesson(db, "One", 1)
    art_svc.classify(db, proj)
    before = model["calls"]

    art_svc.classify(db, proj)
    assert model["calls"] == before


def test_nothing_new_costs_no_model_call(db, model, proj):
    """The common case once a corpus settles. Paying provider quota to re-derive answers
    nobody asked for again is how a scheduled job gets switched off."""
    assert art_svc.classify(db, proj) == [] and model["calls"] == 0


# ---- batching ---------------------------------------------------------------------------------
def test_no_single_call_carries_more_than_the_batch_size(db, model, proj):
    """A single mega-batch of ~100 times out and returns unparseable JSON, so the failure is
    total rather than partial.

    Asserted as a PROPERTY of each call, and with a fixed lesson count. The first version
    sized its fixture from `art_svc.BATCH_SIZE` and asserted a call count — so setting the
    constant to 10,000 built ten thousand lessons and still passed, in 227 seconds. A test
    that scales with the thing it is testing cannot fail."""
    assert art_svc.BATCH_SIZE <= 25, "a mega-batch times out and returns unparseable JSON"
    model["verdicts"] = [{"tier": "fact", "scope": "s", "title": "t"}]
    for i in range(30):
        _lesson(db, "A durable lesson", i)

    art_svc.classify(db, proj)

    assert model["calls"] > 1
    for ctx in model["batches"]:
        sent = json.loads(ctx.split("LESSONS:\n")[1])
        assert len(sent) <= art_svc.BATCH_SIZE


def test_a_failed_batch_does_not_cost_the_others_their_work(db, model, proj):
    """One unparseable reply must not discard the batches that parsed."""
    model["verdicts"] = [{"tier": "fact", "scope": "s", "title": "t"}]
    for i in range(art_svc.BATCH_SIZE + 2):
        _lesson(db, "A durable lesson", i)

    calls = {"n": 0}
    real = model
    from app.services import platform as platform_svc

    class Flaky:
        def chat(self, *, system, context, question, temperature=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return "not json at all"
            ids = [l["id"] for l in json.loads(context.split("LESSONS:\n")[1])]
            return json.dumps([{"id": i, "tier": "fact", "scope": "s", "title": "t"}
                               for i in ids])

    import pytest as _p
    _p.MonkeyPatch().setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Flaky()))
    art_svc.classify(db, proj)

    assert calls["n"] == 2 and len(art_svc.pending(db, proj)) >= 1


def test_later_batches_are_TOLD_what_earlier_ones_created(db, model, proj):
    """A real model cannot answer `update` against something it was never shown, so two
    batches in one run would each `create` for the same subject.

    Asserting on the resulting COUNT does not test this — the database-level supersede
    collapses them regardless, which is why removing the index entirely passed. The claim
    is about what the second call is SHOWN, so that is what is asserted."""
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "Bump it"}]
    for i in range(30):
        _lesson(db, "A durable lesson", i)

    art_svc.classify(db, proj)

    assert len(model["batches"]) > 1
    first_index = model["batches"][0].split("LESSONS:")[0]
    later_index = model["batches"][1].split("LESSONS:")[0]
    assert "Bump it" not in first_index, "nothing existed yet"
    assert "Bump it" in later_index, "the second batch was not shown the first's output"


# ---- it refuses to guess -----------------------------------------------------------------------
def test_with_no_provider_the_model_is_never_called(db, proj, monkeypatch):
    """A tier assigned without a model would put a fabricated verdict in front of a human as
    though something had assessed it.

    Asserted on the CALL, not the result. Checking only that it returns [] passed with the
    guard removed, because the stub's reply happens to be unparseable — so the test proved
    the stub is bad at JSON rather than that we refuse to ask it."""
    from app.services import platform as platform_svc

    calls = {"n": 0}

    class Stub:
        def chat(self, **kw):
            calls["n"] += 1
            return "{}"

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("stub", Stub()))
    _lesson(db, "One", 1)

    assert art_svc.classify(db, proj) == [] and calls["n"] == 0


def test_an_unknown_tier_is_discarded(db, model, proj):
    model["raw"] = json.dumps([{"id": "whatever", "tier": "vibes", "scope": "s"}])
    _lesson(db, "One", 1)

    assert art_svc.classify(db, proj) == []


def test_a_verdict_about_a_lesson_that_was_not_sent_is_discarded(db, model, proj):
    """A hallucinated id would attach evidence to a recommendation that never saw it."""
    model["raw"] = json.dumps([{"id": "m_invented", "tier": "fact", "scope": "s"}])
    _lesson(db, "One", 1)

    assert art_svc.classify(db, proj) == []


def test_an_update_verdict_names_what_it_would_amend(db, model, proj):
    """Scope resolution before creation: if an artifact already owns the subject the verdict
    is an update against it, not a duplicate create."""
    model["verdicts"] = [{"tier": "update", "scope": "migration guard", "title": "x",
                          "target": "AGENTS.md"}]
    _lesson(db, "One", 1)
    art_svc.classify(db, proj)

    assert art_svc.pending(db, proj)[0].target == "AGENTS.md"


def test_existing_artifacts_are_shown_to_the_classifier(db, model, proj):
    """It cannot answer `update` against something it was never told exists — without the
    index every lesson becomes a create."""
    model["verdicts"] = [{"tier": "rule", "scope": "migration guard", "title": "Bump it"}]
    _lesson(db, "One", 1)
    art_svc.classify(db, proj)

    _lesson(db, "Two", 2)
    art_svc.classify(db, proj)
    assert "Bump it" in model["batches"][-1]
