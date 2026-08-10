"""Decide what a promoted lesson should BECOME (GRPH-307 / PRD-16).

The step between "this is true" and "here is a thing you can install". Memory answers the
first; nothing answered the second, so a corpus of correct lessons stayed a corpus.

Three rules carry the design, all of them from PRD-16 and all of them about not producing
work a reviewer then has to undo:

- **Batch the model call.** ~15 lessons per call. A single mega-batch of a hundred times
  out and returns unparseable JSON, so the failure is total rather than partial.
- **Scope resolution BEFORE creation.** If an artifact already owns the lesson's scope the
  verdict is `update` against it, never a duplicate `create`. Two files doing the same job
  is worse than one imperfect file.
- **Only classify what has never been classified.** Re-running over the full set burns
  provider quota and flips reviewed rows back to queued, which quietly discards a human's
  decision — the one thing this pipeline must never do.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArtifactRecommendation, MemoryShard
from app.services import platform as platform_svc

logger = logging.getLogger(__name__)

TIERS = ("fact", "rule", "hook", "skill", "agent", "allowlist", "update", "delete")

# Reference: 15 per call. A single batch of ~100 times out and returns unparseable JSON, so
# the whole run fails rather than one batch of it. A named constant, not a setting — we do
# not know the right number yet, and a slider is how you avoid finding out.
BATCH_SIZE = 15
# Words too common to identify anything. A scope of "the" would match every lesson and turn
# every create into an update against one artifact.
_STOPWORDS = frozenset("""a an and are as at be but by for from has have if in into is it
its of on or that the their then there these this to was were what when which who will
with you your we our us not no do does done use used using can could should would""".split())

CLASSIFY_SYSTEM = (
    "You decide what a durable lesson should BECOME. One verdict per lesson.\n\n"
    "Tiers:\n"
    "  fact       — true, worth remembering, but nothing executes it\n"
    "  rule       — a standing instruction an agent should follow\n"
    "  hook       — something that should run automatically at a point in the workflow\n"
    "  skill      — a procedure worth packaging so it can be invoked deliberately\n"
    "  agent      — a role specialised enough to warrant its own definition\n"
    "  allowlist  — a command or path that should be permitted without asking\n"
    "  delete     — the lesson describes something that should be REMOVED\n\n"
    "You are also given EXISTING artifacts. If one already owns this lesson's subject, "
    "answer `update` and name it in `target`. Be conservative: a wrong match silently "
    "amends the wrong file, which is worse than an honest `create` a human can reject. If "
    "you are not sure it is the same subject, it is not.\n\n"
    "`scope` is two or three words naming what the artifact would OWN — the subject, not a "
    "restatement of the lesson. Two lessons about the same subject must produce the SAME "
    "scope, because that is what stops them becoming two competing artifacts.\n\n"
    'Respond with ONLY a compact JSON array, one object per lesson, in the order given: '
    '[{"id": "<lesson id>", "tier": "...", "scope": "...", "title": "...", '
    '"target": "<existing artifact or null>", "reasoning": "<one sentence>"}]'
)


def _scope_key(scope: str) -> str:
    """Normalise a scope so two phrasings of the same subject collide.

    Without this, "migration guard" and "the migration guards" are different artifacts and
    the deduplication PRD-16 asks for never fires — the check would pass on exact string
    equality, which a model produces roughly never.
    """
    words = re.findall(r"[a-z0-9]+", (scope or "").lower())
    keep = sorted(w.rstrip("s") for w in words if w not in _STOPWORDS and len(w) > 2)
    return " ".join(keep)


def unclassified(db: Session, project_id: str | None = None) -> list[MemoryShard]:
    """Promoted lessons that have never had a recommendation.

    PRD-16 is explicit that re-classifying the full set both burns quota and flips reviewed
    rows back to queued. The second is the serious one: a human said no, and a later run
    would quietly ask again as though they had not.
    """
    from app.services import memory as mem_svc

    seen: set[str] = set()
    for r in db.scalars(select(ArtifactRecommendation)).all():
        seen.update(r.lesson_ids or [])
    return [s for s in mem_svc.list_shards(db, project_id=project_id, status="published")
            if s.id not in seen]


def _existing_index(db: Session, project_id: str | None) -> list[dict]:
    """A keyword-prefiltered index of artifacts that already exist.

    Passed to the model so scope resolution happens BEFORE creation. Without it the model
    cannot know an artifact already owns the subject, and every lesson becomes a create.
    """
    rows = db.scalars(select(ArtifactRecommendation).where(
        ArtifactRecommendation.status.in_(("queued", "approved")))).all()
    if project_id:
        rows = [r for r in rows if r.project_id in (project_id, None)]
    return [{"name": r.title or r.scope, "tier": r.tier, "scope": r.scope} for r in rows]


def classify(db: Session, project_id: str | None = None, *,
             limit: int | None = None) -> list[ArtifactRecommendation]:
    """Turn promoted lessons into artifact recommendations.

    Returns only the rows it created. A run with nothing new to classify is not an error and
    costs no model call — the common case once the corpus settles, and paying provider
    quota to re-derive answers nobody asked for again is how a scheduled job gets disabled.
    """
    lessons = unclassified(db, project_id)
    if limit is not None:
        lessons = lessons[:limit]
    if not lessons:
        return []

    provider, chat = platform_svc.resolve_chat(db, project_id or "core")
    if provider == "stub":
        # No guessing. A tier assigned without a model would put a fabricated verdict in
        # front of a human as though something had assessed it.
        logger.info("artifact classification skipped: no chat provider configured")
        return []

    created: list[ArtifactRecommendation] = []
    index = _existing_index(db, project_id)
    for start in range(0, len(lessons), BATCH_SIZE):
        batch = lessons[start:start + BATCH_SIZE]
        for verdict in _classify_batch(chat, batch, index):
            row = _apply(db, verdict, project_id, graded_by=provider)
            if row is not None:
                created.append(row)
                # Later batches must see what earlier ones produced, or two batches in the
                # same run create competing artifacts for one subject.
                index.append({"name": row.title or row.scope, "tier": row.tier,
                              "scope": row.scope})
    return created


def _classify_batch(chat, batch: list[MemoryShard], index: list[dict]) -> list[dict]:
    """One model call. A batch that fails is skipped, not fatal — a single unparseable
    reply must not cost the other batches their work."""
    context = "\n\n".join([
        "EXISTING ARTIFACTS (answer `update` and name one in `target` if it already owns "
        "the subject):\n" + (json.dumps(index) if index else "(none yet)"),
        "LESSONS:\n" + json.dumps(
            [{"id": s.id, "text": s.text[:600]} for s in batch]),
    ])
    try:
        raw = chat.chat(system=CLASSIFY_SYSTEM, context=context,
                        question="Classify each lesson.", temperature=0)
        match = re.search(r"\[.*\]", raw or "", re.DOTALL)
        parsed = json.loads(match.group(0)) if match else []
    except Exception:  # noqa: BLE001
        logger.warning("artifact classification: unusable reply for a batch of %d",
                       len(batch), exc_info=True)
        return []
    known = {s.id for s in batch}
    out = []
    for v in parsed if isinstance(parsed, list) else []:
        # A verdict about a lesson that was not in the batch is a hallucinated id, and
        # acting on it would attach evidence to a recommendation that never saw it.
        if isinstance(v, dict) and v.get("id") in known and v.get("tier") in TIERS:
            out.append(v)
    return out


def _apply(db: Session, verdict: dict, project_id: str | None,
           graded_by: str) -> ArtifactRecommendation | None:
    """Record one verdict, superseding any existing recommendation for the same subject.

    THE acceptance criterion: two lessons on the same tier and scope produce one
    recommendation, the second superseding the first rather than competing with it. A
    reviewer handed two creates for one subject has to work out which to take, and both
    would install a file doing the same job.

    A row a human has already ANSWERED is never superseded — approving or rejecting is a
    decision, and quietly replacing it would ask again as though it had not been made.
    """
    tier, scope = verdict["tier"], _scope_key(verdict.get("scope", ""))
    lesson_id = verdict["id"]
    prior = db.scalar(select(ArtifactRecommendation).where(
        ArtifactRecommendation.tier == tier,
        ArtifactRecommendation.status == "queued",
    ).order_by(ArtifactRecommendation.id.desc()))
    prior = prior if prior is not None and _scope_key(prior.scope) == scope else None

    row = ArtifactRecommendation(
        project_id=project_id,
        tier=tier,
        scope=verdict.get("scope", ""),
        title=str(verdict.get("title") or "")[:200],
        reasoning=str(verdict.get("reasoning") or ""),
        # Carries the earlier evidence forward: the drafting step re-renders from the
        # CURRENT lesson set, and a superseding row that forgot its predecessors would
        # render a weaker artifact than the one it replaced.
        lesson_ids=sorted(set((prior.lesson_ids or []) + [lesson_id])) if prior
        else [lesson_id],
        target=verdict.get("target") or None,
        graded_by=graded_by,
        supersedes_id=prior.id if prior else None,
    )
    if prior is not None:
        prior.status = "superseded"
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def pending(db: Session, project_id: str | None = None) -> list[ArtifactRecommendation]:
    """Recommendations awaiting a human. Superseded rows are excluded — they are history,
    not a queue — but they are kept, because how a decision was reached is the part you
    cannot reconstruct later."""
    rows = db.scalars(select(ArtifactRecommendation).where(
        ArtifactRecommendation.status == "queued").order_by(
        ArtifactRecommendation.id)).all()
    return [r for r in rows if project_id is None or r.project_id in (project_id, None)]


# ---- drafting and the human boundary (GRPH-308 / PRD-16) ---------------------------------
# Where each tier's artifact lives, and — the load-bearing half — whether it may be written
# at all. `file_additive` is a wholly new self-contained file. `shared_surgery` is an edit
# inside a file many other things live in, and is NEVER written however confident anything
# is: PRD-16's non-goal says generated artifacts are proposed and the human boundary does
# not move, and a machine editing AGENTS.md is the move that loses trust once and keeps it
# lost.
TIER_TARGETS: dict[str, tuple[str, str]] = {
    "skill":     (".claude/skills/{slug}/SKILL.md", "file_additive"),
    "agent":     (".claude/agents/{slug}.md", "file_additive"),
    "hook":      (".claude/hooks/{slug}.sh", "file_additive"),
    "fact":      ("", "shared_surgery"),      # memory already holds it; nothing to install
    "rule":      ("AGENTS.md", "shared_surgery"),
    "allowlist": (".claude/settings.json", "shared_surgery"),
    "update":    ("", "shared_surgery"),      # amends something a human already owns
    "delete":    ("", "shared_surgery"),
}

DRAFT_SYSTEM = (
    "You render the REAL artifact — the file someone could install today, not a summary of "
    "it and not a TODO. Output only the artifact's contents. No preamble, no code fence, "
    "no explanation.\n\n"
    "skill     — a complete SKILL.md: YAML frontmatter with `name` and `description`, then "
    "the procedure as numbered steps someone can follow without you.\n"
    "agent     — a complete agent definition: frontmatter with `name`, `description` and "
    "`tools`, then the system prompt.\n"
    "hook      — a runnable POSIX shell script starting with a shebang, plus its "
    "registration snippet in a trailing comment.\n"
    "rule      — the rule TEXT only, no file format and no heading: the adapter that owns "
    "the target file decides how to wrap it.\n"
    "allowlist — the exact entries to permit, one per line, nothing else.\n"
    "fact/update/delete — one paragraph stating precisely what should change and why.\n\n"
    "Write nothing you cannot support from the lessons given. An artifact that invents a "
    "step is worse than a short one, because a reviewer cannot tell which parts were "
    "earned."
)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:5]) or "artifact"


def _draft_hash(rec: ArtifactRecommendation, lessons: list[MemoryShard]) -> str:
    """sha256 of (statement + evidence).

    Keyed on the lesson TEXT, not on ids: a lesson that was edited should re-draft, and one
    that merely got a new id should not. That is what makes "unchanged costs zero calls"
    true rather than approximately true.
    """
    import hashlib

    body = " ".join([rec.tier, rec.scope, rec.title] + sorted(s.text for s in lessons))
    return hashlib.sha256(body.encode()).hexdigest()


def _provenance(rec: ArtifactRecommendation, lessons: list[MemoryShard]) -> str:
    """The footer, computed from the store and never asked of the model.

    PRD-16 is specific about this and the reason is not pedantry: a model asked to state how
    many lessons back an artifact will produce a plausible number, and a plausible number
    that disagrees with the record is worse than none — it makes the provenance itself
    untrustworthy, which is the one thing this footer exists to establish.
    """
    sources = {s.source for s in lessons if s.source}
    return (f"\n\n<!-- generated by graphban · {rec.tier} · "
            f"slug {_slug(rec.title or rec.scope)} · {len(lessons)} lesson(s) from "
            f"{len(sources) or 'unknown'} source(s) · recommendation #{rec.id} -->")


def draft(db: Session, rec: ArtifactRecommendation) -> ArtifactRecommendation:
    """Render one recommendation into an installable artifact.

    One model call per recommendation — quality over volume, and unlike classification there
    is nothing to batch: each artifact is a different document.

    **Re-drafts only when the lessons changed.** An unchanged set reuses the stored draft and
    costs nothing, which is what lets a scheduled pass stay switched on rather than being
    disabled the first time somebody reads the provider bill.
    """
    lessons = [s for s in (db.get(MemoryShard, i) for i in (rec.lesson_ids or []))
               if s is not None]
    fingerprint = _draft_hash(rec, lessons)
    if rec.draft and rec.draft_hash == fingerprint:
        return rec

    path_tpl, install_class = TIER_TARGETS.get(rec.tier, ("", "shared_surgery"))
    provider, chat = platform_svc.resolve_chat(db, rec.project_id or "core")
    if provider == "stub":
        # No guessing. A drafted artifact is a file someone may install; producing one
        # without a model would put fabricated content behind a real install button.
        logger.info("artifact drafting skipped: no chat provider configured")
        return rec

    context = "\n\n".join([
        f"TIER: {rec.tier}", f"SUBJECT: {rec.scope}", f"TITLE: {rec.title}",
        "LESSONS (everything you may draw on):",
        "\n".join(f"- {s.text}" for s in lessons) or "(none)",
    ])
    try:
        body = chat.chat(system=DRAFT_SYSTEM, context=context,
                         question="Render the artifact.", temperature=0).strip()
    except Exception:  # noqa: BLE001 — one failed draft must not end a run
        logger.warning("artifact drafting failed for recommendation %s", rec.id,
                       exc_info=True)
        return rec
    if not body:
        return rec

    rec.draft = body + _provenance(rec, lessons)
    rec.draft_path = path_tpl.format(slug=_slug(rec.title or rec.scope)) if path_tpl else ""
    rec.install_class = install_class
    rec.draft_hash = fingerprint
    db.commit()
    db.refresh(rec)
    return rec


def draft_pending(db: Session, project_id: str | None = None) -> list[ArtifactRecommendation]:
    """Draft everything queued. One call each, and none for anything already current."""
    return [draft(db, r) for r in pending(db, project_id)]


class InstallRefused(RuntimeError):
    """A recommendation that may not be written, and why."""


def install_plan(db: Session, rec: ArtifactRecommendation) -> dict:
    """What installing this would do — and whether it may happen at all.

    Returns `{allowed, install_class, path, contents, reason}`. `allowed: False` is not a
    failure: it is the DESIGNED outcome for anything touching a file a human owns, and it
    still returns the full contents so the change can be applied by hand. Refusing without
    handing back the work would just mean the artifact was never drafted.

    Machine-owned artifacts update by FULL RE-RENDER from the current lesson set, never by
    patch — patching accumulates sediment, and an artifact nobody can read is one nobody
    trusts enough to keep.
    """
    if not rec.draft:
        raise InstallRefused("nothing drafted yet")
    if rec.install_class != "file_additive":
        return {
            "allowed": False,
            "install_class": rec.install_class or "shared_surgery",
            "path": rec.draft_path or (rec.target or ""),
            "contents": rec.draft,
            "reason": ("this edits a file other things live in, so it is proposed rather "
                       "than written — apply the contents by hand"),
        }
    return {
        "allowed": True,
        "install_class": "file_additive",
        "path": rec.draft_path,
        "contents": rec.draft,
        "reason": "",
    }
