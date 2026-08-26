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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ArtifactRecommendation, ArtifactTombstone, ArtifactUsage, MemoryShard,
)
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
            if s.id not in seen and not _is_unresolved(s)]


def _is_unresolved(shard) -> bool:
    """An ingested failure that nothing ever fixed (GRPH-349).

    Kept out of classification because a rule can only be drafted from what was done
    DIFFERENTLY, and an unresolved failure records no such thing. Asked to generalise from
    one anyway, the drafter invents a plausible cause — `cd:1: no such file or directory`
    became "ensure the directory exists", drafted as a shell guard, for a directory that
    exists.
    """
    origin = shard.origin or ""
    return origin.endswith(":unresolved") or origin.endswith(":transient")


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

    _r = platform_svc.resolve_chat(db, project_id or "core")
    provider, chat = _r.provider_id, _r.chat
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
    _r = platform_svc.resolve_chat(db, rec.project_id or "core")
    provider, chat = _r.provider_id, _r.chat
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
    if rec.tier == "hook":
        # Instrumented AT GENERATION (GRPH-309). We render the script, so it can report its
        # own firing — the artifact saying what it did, rather than us inferring use from
        # transcript silence. That is what makes hooks measurable when rules cannot be.
        rec.draft += HOOK_TELEMETRY.format(rec_id=rec.id)
    rec.draft_path = path_tpl.format(slug=_slug(rec.title or rec.scope)) if path_tpl else ""
    rec.install_class = install_class
    rec.draft_hash = fingerprint
    db.commit()
    db.refresh(rec)
    return rec


def draft_pending(db: Session, project_id: str | None = None) -> list[ArtifactRecommendation]:
    """Draft everything queued. One call each, and none for anything already current.

    A recommendation that fails to draft is skipped rather than ending the pass (GRPH-353).
    `draft` already swallows a failed MODEL call, which was enough while the only caller was
    a test; under a scheduled driver the rest matters too — a lesson row that vanished, a
    commit that lost a race — and one of those must not cost the other twenty-nine drafts
    their run. The rollback is the load-bearing half: without it a failed write leaves the
    session poisoned and every later `draft` dies with PendingRollbackError, so a single bad
    row still ends the pass, just noisily rather than cleanly.
    """
    out = []
    for rec in pending(db, project_id):
        try:
            out.append(draft(db, rec))
        except Exception:  # noqa: BLE001 — one bad recommendation must not end the run
            db.rollback()
            logger.warning("artifact drafting: skipping recommendation %s", rec.id,
                           exc_info=True)
    return out


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
    from app.services import artifact_inventory as inv_svc

    if not rec.draft:
        raise InstallRefused("nothing drafted yet")
    # A human has edited this artifact since we rendered it (GRPH-354). Updates are FULL
    # re-renders, so writing here would discard their edit and report success — the precise
    # trust failure the propose-only boundary exists to prevent. The contents still come
    # back, because a refusal that withheld them would just mean the work was never done.
    forked = inv_svc.fork_of(db, rec)
    if forked is not None:
        return {
            "allowed": False,
            "install_class": rec.install_class or "file_additive",
            "path": forked.path or rec.draft_path or "",
            "contents": rec.draft,
            "reason": ("this artifact has been edited by hand since it was generated, and an "
                       "update would re-render it in full — reconcile the two by hand"),
        }
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


# ---- usage telemetry and retirement (GRPH-309 / PRD-16) ----------------------------------
# Which tiers can be observed being used, and which cannot. Settled per-tier on 2026-08-10
# rather than uniformly, because the two halves fail differently: a stale RULE is clutter,
# while a stale HOOK still runs, still costs time, and may still block something.
#
#   skill / agent — first-party signal. Graphban already meters MCP calls by name.
#   hook          — instrumented AT GENERATION. We render the script, so it reports its own
#                   firing: the artifact saying what it did, not us inferring from silence.
#   rule          — UNMEASURABLE, accepted. A rule that works is one an agent silently
#                   complied with, and compliance leaves no trace by construction.
#
# Anything not measurable is excluded from staleness entirely. Reporting it at zero use
# would read as "unused" when it means "unobservable", and that reading deletes working
# artifacts — PRD-16 says so in as many words.
MEASURABLE_TIERS = ("skill", "agent", "hook")
UNMEASURABLE_TIERS = ("rule", "allowlist", "fact", "update", "delete")

# How long an artifact must go unused before retirement is even proposed. A named constant,
# not a setting; 30 days matches PRD-16's survival metric so the two cannot disagree.
STALE_AFTER_DAYS = 30

# Appended to every generated hook so it reports its own firing. One line, no dependencies,
# and it fails silently — a hook that breaks the workflow because telemetry is unreachable
# is a hook that gets deleted by hand, which is the outcome this whole feature exists to
# make deliberate rather than accidental.
HOOK_TELEMETRY = (
    '\n# --- graphban usage telemetry (GRPH-309): reports THIS hook fired. Never fails the\n'
    '# --- hook: a telemetry outage must not break the workflow it is measuring.\n'
    'curl -fsS -m 2 -X POST "$GRAPHBAN_URL/api/artifacts/{rec_id}/used" \\\n'
    '  -H "X-API-Key: $GRAPHBAN_KEY" >/dev/null 2>&1 || true\n'
)


def record_use(db: Session, recommendation_id: int, signal: str) -> ArtifactUsage:
    """Record an OBSERVED use. Never called on inference."""
    row = ArtifactUsage(recommendation_id=recommendation_id, signal=signal)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def measurable(rec: ArtifactRecommendation) -> bool:
    return rec.tier in MEASURABLE_TIERS


def usage_report(db: Session, project_id: str | None = None) -> dict:
    """Population and usage, with the unmeasurable half named rather than hidden.

    PRD-16 asks that unmeasurable artifacts still appear in POPULATION counts — they exist,
    they cost context, and a report that omitted them would understate the corpus. What they
    must never appear in is a staleness figure, because "no uses recorded" and "uses cannot
    be recorded" are opposite claims and only one of them is a reason to delete something.
    """
    from app.services import artifact_inventory as inv_svc

    rows = [r for r in db.scalars(select(ArtifactRecommendation).where(
        ArtifactRecommendation.status == "approved")).all()
        if project_id is None or r.project_id in (project_id, None)]
    counts: dict[int, int] = {}
    for u in db.scalars(select(ArtifactUsage)).all():
        counts[u.recommendation_id] = counts.get(u.recommendation_id, 0) + 1

    out = []
    for r in rows:
        out.append({
            "id": r.id, "tier": r.tier, "title": r.title, "path": r.draft_path,
            "measurable": measurable(r),
            # None, not 0. A zero would be a measurement nobody took.
            "uses": counts.get(r.id, 0) if measurable(r) else None,
            "reason": "" if measurable(r) else
                      "usage cannot be observed for this tier — compliance leaves no trace",
            "origin": "generated",
        })

    # Everything on disk this pipeline did NOT write (GRPH-354). Without these the report
    # measured its own footprint: a fresh install showed a population of zero while the
    # operator's `.claude/` directory held dozens of artifacts, and those are the ones
    # actually spending context on every turn.
    #
    # Discovered artifacts are NEVER measurable, whatever their tier. A generated skill is
    # measurable because Graphban meters its own MCP calls by name and instruments the hooks
    # it renders; a hand-written one has no such instrumentation, so `uses: 0` there would be
    # a measurement nobody took — and zero uses on a measurable tier is what queues a delete.
    found = inv_svc.inventory(db, project_id, include_orphaned=True)
    for row in found:
        if row.recommendation_id is not None:
            continue  # already counted above as the generated artifact it is
        out.append({
            "id": None, "inventory_id": row.id, "tier": row.tier,
            "title": row.path.rsplit("/", 1)[-1], "path": row.path,
            "measurable": False,
            "uses": None,
            "reason": ("discovered on disk rather than generated — Graphban has no "
                       "instrumentation in it, so its use cannot be observed"),
            "origin": "discovered",
            "state": row.state,
        })

    return {
        "artifacts": out,
        "population": len(out),
        "generated": sum(1 for r in out if r["origin"] == "generated"),
        "discovered": sum(1 for r in out if r["origin"] == "discovered"),
        # Counted from the INVENTORY, not from `out`. A forked row always carries a
        # `recommendation_id` and so is skipped by the loop above — counting these off `out`
        # gave a `forked` figure that could never be anything but zero, which is the same
        # "absence reads as a clean result" this whole feature exists to close.
        "orphaned": sum(1 for r in found if r.state == "orphaned"),
        "forked": sum(1 for r in found if r.state == "forked"),
        "measurable": sum(1 for r in out if r["measurable"]),
        # Named explicitly so a reader can see what the numbers below do NOT cover.
        "unmeasurable": [r["title"] or r["path"] for r in out if not r["measurable"]],
    }


def stale_artifacts(db: Session, project_id: str | None = None,
                    now: datetime | None = None) -> list[ArtifactRecommendation]:
    """Approved, measurable artifacts with zero observed uses across the window.

    Unmeasurable tiers are excluded before anything is counted, not filtered out afterwards
    — the difference matters, because a later refactor that drops the filter would otherwise
    start proposing deletions for rules on the strength of a count that was never taken.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=STALE_AFTER_DAYS)
    used = {u.recommendation_id for u in db.scalars(select(ArtifactUsage)).all()}
    out = []
    for r in db.scalars(select(ArtifactRecommendation).where(
            ArtifactRecommendation.status == "approved")).all():
        if project_id is not None and r.project_id not in (project_id, None):
            continue
        if not measurable(r):
            continue
        created = r.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        # An artifact installed yesterday has not had a chance to be used. Proposing its
        # retirement would train a reviewer to dismiss the whole queue.
        if created <= cutoff and r.id not in used:
            out.append(r)
    return out


def retire(db: Session, rec: ArtifactRecommendation, *, reason: str = "") -> ArtifactTombstone:
    """Archive an artifact with everything needed to bring it back.

    Never an unrecoverable delete. The full contents are kept, because a retirement that
    discarded them would make the decision irreversible on the strength of a usage count —
    and a usage count is exactly the kind of evidence that turns out to have been measuring
    the wrong thing.

    Refuses outright for an unmeasurable tier. Not a filter that happens to exclude them: a
    hard refusal, so no future caller can retire a rule by passing it in directly.
    """
    if not measurable(rec):
        raise InstallRefused(
            f"{rec.tier} usage cannot be observed, so zero uses is not evidence of disuse")
    uses = len([u for u in db.scalars(select(ArtifactUsage)).all()
                if u.recommendation_id == rec.id])
    stone = ArtifactTombstone(
        recommendation_id=rec.id,
        path=rec.draft_path,
        contents=rec.draft,
        use_count=uses,
        reason=reason or f"no observed use in {STALE_AFTER_DAYS} days",
    )
    rec.status = "retired"
    db.add(stone)
    db.commit()
    db.refresh(stone)
    return stone


def restore(db: Session, stone: ArtifactTombstone) -> dict:
    """The one-command restore PRD-16 asks for: path and contents, ready to write back."""
    rec = db.get(ArtifactRecommendation, stone.recommendation_id)
    if rec is not None:
        rec.status = "approved"
        db.commit()
    return {"path": stone.path, "contents": stone.contents,
            "recommendation_id": stone.recommendation_id}
