"""Drafting the real artifact, and the human boundary (GRPH-308 / PRD-16).

*"Render the real artifact — the thing someone could install today, not a summary or a
TODO."* A recommendation that says "you should write a skill for this" moves no work; the
skill does.

The load-bearing half is the install policy, and it is a property of the TARGET rather than
of the artifact's quality:

- `file_additive` — a wholly new self-contained file, may install on approval;
- `shared_surgery` — an edit inside a file many other things live in, **never written**.

PRD-16's non-goal says generated artifacts are proposed and the human boundary does not
move in this PRD. A machine editing `AGENTS.md` is the move that loses trust once and keeps
it lost, so a perfect edit to a shared file is still refused.
"""
import pytest

from app.models import ArtifactRecommendation, Project
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
def proj(db):
    db.add(Project(id="drafting", name="Drafting", tag="DR"))
    db.commit()
    return "drafting"


@pytest.fixture()
def model(monkeypatch):
    from app.services import platform as platform_svc

    state = {"body": "# Rendered artifact\n\nDo the thing.", "calls": 0, "boom": False,
             "contexts": []}

    class Chat:
        def chat(self, *, system, context, question, temperature=None):
            state["calls"] += 1
            state["contexts"].append(context)
            if state["boom"]:
                raise RuntimeError("drafter down")
            return state["body"]

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    return state


def _rec(db, proj, tier="skill", scope="migration guard", lessons=()):
    ids = []
    for i, text in enumerate(lessons or ["Always bump the migration range."]):
        ids.append(mem_svc.add_memory(db, text_body=f"{text} ({i})", project_id=proj,
                                      status="published").id)
    rec = ArtifactRecommendation(project_id=proj, tier=tier, scope=scope,
                                 title="Bump the migration range", lesson_ids=ids)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


# ---- it renders the artifact itself ---------------------------------------------------------
def test_a_skill_recommendation_yields_an_installable_file(db, proj, model):
    """THE acceptance criterion. A recommendation saying "write a skill for this" moves no
    work; the skill does."""
    rec = art_svc.draft(db, _rec(db, proj, tier="skill"))

    assert rec.draft.startswith("# Rendered artifact")
    assert rec.draft_path == ".claude/skills/bump-the-migration-range/SKILL.md"
    assert rec.install_class == "file_additive"


def test_the_lessons_are_what_the_drafter_is_given(db, proj, model):
    """It may draw on the evidence and nothing else — an artifact that invents a step is
    worse than a short one, because a reviewer cannot tell which parts were earned."""
    art_svc.draft(db, _rec(db, proj, lessons=["Bump the range in AGENTS.md"]))

    assert "Bump the range in AGENTS.md" in model["contexts"][0]


def test_each_tier_lands_where_it_belongs(db, proj, model):
    for tier, expected in (("skill", ".claude/skills/"), ("agent", ".claude/agents/"),
                           ("hook", ".claude/hooks/")):
        rec = art_svc.draft(db, _rec(db, proj, tier=tier, scope=f"s-{tier}"))
        assert rec.draft_path.startswith(expected)


# ---- the human boundary ----------------------------------------------------------------------
def test_a_rule_targeting_a_shared_file_refuses_to_install(db, proj, model):
    """THE other acceptance criterion. A rule belongs in AGENTS.md, which a human owns and
    many other things live in — so it is proposed, never written."""
    rec = art_svc.draft(db, _rec(db, proj, tier="rule"))
    plan = art_svc.install_plan(db, rec)

    assert plan["allowed"] is False
    assert plan["install_class"] == "shared_surgery"
    assert "apply the contents by hand" in plan["reason"]


def test_a_refused_install_still_hands_back_the_work(db, proj, model):
    """Refusing without returning the contents would mean the artifact was never drafted.
    The point is to move the work to a human, not to withhold it."""
    rec = art_svc.draft(db, _rec(db, proj, tier="rule"))
    plan = art_svc.install_plan(db, rec)

    assert plan["contents"].startswith("# Rendered artifact")
    assert plan["path"] == "AGENTS.md"


def test_an_additive_file_may_install(db, proj, model):
    plan = art_svc.install_plan(db, art_svc.draft(db, _rec(db, proj, tier="skill")))
    assert plan["allowed"] is True and plan["path"].endswith("SKILL.md")


@pytest.mark.parametrize("tier", ["rule", "allowlist", "update", "delete", "fact"])
def test_every_shared_target_is_refused(db, proj, model, tier):
    """Not a judgement about quality. A perfect edit to a file other things live in is
    still an edit to a file other things live in."""
    rec = art_svc.draft(db, _rec(db, proj, tier=tier, scope=f"s-{tier}"))
    assert art_svc.install_plan(db, rec)["allowed"] is False


def test_an_unknown_tier_defaults_to_refusing(db, proj, model):
    """Fail closed. A tier nobody anticipated must not become a write."""
    rec = _rec(db, proj, tier="fact")
    rec.tier = "something-new"
    db.commit()
    art_svc.draft(db, rec)

    assert rec.install_class == "shared_surgery"


def test_installing_something_undrafted_is_refused(db, proj):
    with pytest.raises(art_svc.InstallRefused, match="nothing drafted"):
        art_svc.install_plan(db, _rec(db, proj))


# ---- cost -------------------------------------------------------------------------------------
def test_an_unchanged_lesson_set_costs_no_second_call(db, proj, model):
    """What lets a scheduled pass stay switched on rather than being disabled the first time
    somebody reads the provider bill."""
    rec = _rec(db, proj)
    art_svc.draft(db, rec)
    art_svc.draft(db, rec)
    art_svc.draft(db, rec)

    assert model["calls"] == 1


def test_an_edited_lesson_re_drafts(db, proj, model):
    """The hash is over lesson TEXT, not ids: a lesson that was edited should re-draft, and
    one that merely got a new id should not."""
    rec = _rec(db, proj)
    art_svc.draft(db, rec)
    shard = db.get(mem_svc.MemoryShard, rec.lesson_ids[0])
    shard.text = "Something materially different was learned."
    db.commit()

    art_svc.draft(db, rec)
    assert model["calls"] == 2


# ---- provenance is computed, never asked for ---------------------------------------------------
def test_the_footer_is_computed_from_the_store(db, proj, model):
    """A model asked how many lessons back an artifact produces a PLAUSIBLE number, and a
    plausible number that disagrees with the record is worse than none — it makes the
    provenance itself untrustworthy, which is the one thing the footer establishes."""
    rec = art_svc.draft(db, _rec(db, proj, lessons=["One", "Two", "Three"]))

    assert "3 lesson(s)" in rec.draft
    assert f"recommendation #{rec.id}" in rec.draft


def test_the_model_is_not_asked_for_the_count(db, proj, model):
    """If the model wrote the footer it could disagree with the store and nobody would know
    which was right."""
    model["body"] = "# Artifact\n\n<!-- generated from 99 lessons -->"
    rec = art_svc.draft(db, _rec(db, proj, lessons=["One", "Two"]))

    assert "2 lesson(s)" in rec.draft, "the store's count must be present"


# ---- it refuses to guess -------------------------------------------------------------------------
def test_with_no_provider_nothing_is_drafted(db, proj, monkeypatch):
    """A drafted artifact is a file someone may install. Producing one without a model would
    put fabricated content behind a real install button."""
    from app.services import platform as platform_svc

    calls = {"n": 0}

    class Stub:
        def chat(self, **kw):
            calls["n"] += 1
            return "whatever"

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("stub", Stub()))
    rec = art_svc.draft(db, _rec(db, proj))

    assert rec.draft == "" and calls["n"] == 0


def test_a_failed_draft_leaves_the_recommendation_alone(db, proj, model):
    """One failure must not end a run, and must not leave a half-written artifact behind a
    button that says install."""
    model["boom"] = True
    rec = art_svc.draft(db, _rec(db, proj))

    assert rec.draft == "" and rec.install_class == ""


def test_an_empty_reply_is_not_stored_as_an_artifact(db, proj, model):
    model["body"] = "   "
    rec = art_svc.draft(db, _rec(db, proj))

    assert rec.draft == ""


# ---- only a resolved episode is a lesson (GRPH-349) ------------------------------------------
def _ingested(db, proj, origin, text="Tool: Bash\nAttempted: x\nFailed: y\nResolved by: z"):
    return mem_svc.add_memory(db, text_body=text, project_id=proj, status="published",
                              origin=origin, source="transcript:claude-code:s1")


@pytest.mark.parametrize("origin", ["ingest:claude-code:unresolved",
                                    "ingest:claude-code:transient"])
def test_a_failure_with_no_fix_is_never_classified(db, proj, origin):
    """A rule can only be drafted from what was done DIFFERENTLY, and neither of these
    records one. Asked to generalise from a bare failure the drafter invents a cause:
    `cd:1: no such file or directory` became "ensure the directory exists", rendered as a
    shell guard, for a directory that exists."""
    _ingested(db, proj, origin)
    assert art_svc.unclassified(db, proj) == []


def test_a_resolved_episode_is_classified(db, proj):
    """The other half — a filter that excluded everything would pass the test above."""
    rec = _ingested(db, proj, "ingest:claude-code:resolved")
    assert [s.id for s in art_svc.unclassified(db, proj)] == [rec.id]


def test_an_ordinary_lesson_is_unaffected(db, proj):
    """Nothing about hand-written or grill-derived memory changes."""
    rec = mem_svc.add_memory(db, text_body="Always bump the migration range.",
                             project_id=proj, status="published", origin="user:alex")
    assert rec.id in [s.id for s in art_svc.unclassified(db, proj)]
