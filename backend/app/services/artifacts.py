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
